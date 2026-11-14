"""Provider-neutral original module-definition identity seals.

Seals describe executable source, not permission, data authenticity, or budget.
Only the actual module loader's executing definition may register itself once.
"""

from __future__ import annotations

import copyreg
import inspect
import sys
from types import ModuleType
from typing import Any

from .errors import ReviewInputError


def _build_registry():
    registered: dict[Any, tuple[tuple[Any, str, Any, Any], ...]] = {}

    classes: dict[type, Any] = {}
    function_defaults: dict[Any, Any] = {}
    function_wrappers: dict[Any, Any] = {}
    function_definitions: dict[Any, Any] = {}
    no_wrapper = object()

    def default_snapshot(value, ancestors=()):
        # Retain both binding identity and container contents. In particular the
        # kwdefaults dictionary can be mutated without replacing the function.
        identity = id(value)
        if identity in ancestors:
            return (identity, "cycle")
        ancestors = (*ancestors, identity)
        if type(value) in (type(None), bool, int, float, str, bytes):
            return (identity, type(value), repr(value))
        if type(value) in (tuple, list):
            return (identity, tuple(default_snapshot(v, ancestors) for v in value))
        if type(value) is dict:
            return (
                identity,
                tuple(
                    (default_snapshot(k, ancestors), default_snapshot(v, ancestors))
                    for k, v in value.items()
                ),
            )
        if type(value) in (set, frozenset):
            return (identity, frozenset(default_snapshot(v, ancestors) for v in value))
        return (identity, getattr(value, "__code__", None))

    def defaults(function):
        return (
            default_snapshot(function.__defaults__),
            default_snapshot(function.__kwdefaults__),
        )

    def normalize(value):
        return getattr(value, "__func__", value)

    def remember_function(function):
        # inspect.unwrap follows mutable function metadata. Pin each original
        # link and inner definition, not just the visible wrapper's code.
        seen = set()
        while True:
            if not inspect.isfunction(function) or function in seen:
                raise ReviewInputError("original source wrapper chain is invalid")
            seen.add(function)
            if function in function_defaults:
                return  # Never replace an earlier defining-module snapshot.
            wrapped = vars(function).get("__wrapped__", no_wrapper)
            function_defaults[function] = defaults(function)
            function_wrappers[function] = wrapped
            function_definitions[function] = (
                function.__code__,
                function.__globals__,
            )
            if wrapped is no_wrapper:
                return
            function = wrapped

    def function_unchanged(function):
        seen = set()
        while function not in seen:
            seen.add(function)
            definition = function_definitions.get(function)
            if (
                definition is None
                or function.__code__ is not definition[0]
                or function.__globals__ is not definition[1]
                or defaults(function) != function_defaults[function]
                or vars(function).get("__wrapped__", no_wrapper)
                is not function_wrappers[function]
            ):
                return False
            wrapped = function_wrappers[function]
            if wrapped is no_wrapper:
                return True
            function = wrapped
        return False

    def seal_module():
        current = inspect.currentframe()
        frame = current.f_back if current is not None else None
        if frame is None:
            raise ReviewInputError("original source definition frame is unavailable")
        name = frame.f_globals.get("__name__")
        if not isinstance(name, str):
            raise ReviewInputError("original source definition name is unavailable")
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        get_code = getattr(loader, "get_code", None)
        if (
            not isinstance(module, ModuleType)
            or vars(module) is not frame.f_globals
            or frame.f_locals is not frame.f_globals
            or frame.f_code.co_name != "<module>"
            or not getattr(spec, "_initializing", False)
            or not callable(get_code)
            or get_code(name) != frame.f_code
            or module in registered
        ):
            raise ReviewInputError("original source seal requires its defining module")
        bindings: list[tuple[Any, str, Any, Any]] = []
        for key, value in tuple(vars(module).items()):
            if key.isupper() and isinstance(
                value, (str, int, float, bool, tuple, frozenset)
            ):
                bindings.append((module, key, value, None))
                continue
            if not (inspect.isfunction(value) or inspect.isclass(value)) or not getattr(
                value, "__module__", ""
            ).startswith("review_sensei"):
                continue
            bindings.append((module, key, value, getattr(value, "__code__", None)))
            if inspect.isclass(value) and value.__module__ == name:
                # copy.copy lazily materializes this stdlib metadata on the
                # class. Compute it at definition time, without constructing or
                # copying an application object, then seal its binding/content.
                slot_names = getattr(copyreg, "_slotnames")(value)
                if type(slot_names) is not list or any(
                    not isinstance(slot, str) for slot in slot_names
                ):
                    raise ReviewInputError("original class slot metadata is invalid")
                members: list[tuple[Any, str, Any, Any]] = []
                original_methods = {}
                executable = set()
                for attribute in dir(value):
                    member = normalize(inspect.getattr_static(value, attribute))
                    if isinstance(member, property) or callable(member):
                        executable.add(attribute)
                        row = (
                            value,
                            attribute,
                            member,
                            getattr(member, "__code__", None),
                        )
                        members.append(row)
                        if callable(member):
                            original_methods[attribute] = member
                    if isinstance(member, property):
                        for accessor in ("fget", "fset", "fdel"):
                            function = getattr(member, accessor)
                            if function is not None:
                                members.append(
                                    (member, accessor, function, function.__code__)
                                )
                classes[value] = (
                    module,
                    str(getattr(value, "__name__", key))
                    if getattr(module, str(getattr(value, "__name__", key)), None)
                    is value
                    else key,
                    frozenset(vars(value)),
                    frozenset(executable),
                    tuple(members),
                    original_methods,
                    (slot_names, tuple(slot_names)),
                )
                bindings.extend(members)
        for _owner, _name, original, _code in bindings:
            if inspect.isfunction(original):
                remember_function(original)
        registered[module] = tuple(bindings)

    def require_bindings(bindings):
        for owner, name, original, code in bindings:
            current = normalize(
                inspect.getattr_static(owner, name, None)
                if inspect.isclass(owner)
                else getattr(owner, name, None)
            )
            if (
                current is not original
                or getattr(current, "__code__", None) is not code
                or (inspect.isfunction(current) and not function_unchanged(current))
            ):
                raise ReviewInputError("original module source changed: " + name)

    def require_class(cls):
        original = classes.get(cls)
        if original is None:
            raise ReviewInputError("original source class is unavailable")
        module, name, namespace, executable, bindings, _methods, slots = original
        slot_values = slots[0]
        if (
            sys.modules.get(module.__name__) is not module
            or getattr(module, name, None) is not cls
            or frozenset(vars(cls)) != namespace
            or vars(cls).get("__slotnames__") is not slots[0]
            or type(slot_values) is not list
            or tuple(slot_values) != slots[1]
            or frozenset(
                name
                for name in dir(cls)
                if callable(normalize(inspect.getattr_static(cls, name)))
                or isinstance(inspect.getattr_static(cls, name), property)
            )
            != executable
        ):
            raise ReviewInputError("original class source changed")
        require_bindings(bindings)

    def require_module_seal(module):
        bindings = registered.get(module) if isinstance(module, ModuleType) else None
        if bindings is None or sys.modules.get(module.__name__) is not module:
            raise ReviewInputError("original source module seal is unavailable")
        require_bindings(bindings)
        for cls, source in classes.items():
            if source[0] is module:
                require_class(cls)

    def require_instance_source(instance, *, methods):
        cls = type(instance)
        for base in cls.__mro__:
            if base is not object:
                require_class(base)
        source = classes.get(cls)
        if source is None:
            raise ReviewInputError("original source class is unavailable")
        original_methods = source[5]
        if not isinstance(methods, tuple) or any(
            not isinstance(name, str)
            or name not in original_methods
            or name in instance.__dict__
            or normalize(getattr(instance, name, None)) is not original_methods[name]
            for name in methods
        ):
            raise ReviewInputError("original source instance method changed")

    return seal_module, require_module_seal, require_instance_source


seal_module, require_module_seal, require_instance_source = _build_registry()
del _build_registry
seal_module()
