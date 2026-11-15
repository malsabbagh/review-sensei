"""Provider-neutral original module-definition identity seals.

Seals describe executable source, not permission, data authenticity, or budget.
Only the actual module loader's executing definition may register itself once.
"""

from __future__ import annotations

import copyreg
import inspect
import sys
from types import FunctionType, MethodType, ModuleType
from typing import Any

from .errors import ReviewInputError


def _build_registry():
    registered: dict[Any, tuple[tuple[Any, str, Any, Any], ...]] = {}

    classes: dict[type, Any] = {}
    module_classes: dict[ModuleType, tuple[type, ...]] = {}
    function_defaults: dict[Any, Any] = {}
    function_wrappers: dict[Any, Any] = {}
    function_definitions: dict[Any, Any] = {}
    no_wrapper = object()
    immutable_default = object()
    ancestor_default = object()

    def default_snapshot(value, ancestors=()):
        # Retain original identities as well as container contents. Holding the
        # originals prevents a replacement from recycling a recorded object ID.
        # This describes definitions, never a previous validation result.
        identity = id(value)
        kind = type(value)
        if identity in ancestors:
            return (value, kind, ancestor_default)
        ancestors = (*ancestors, identity)
        if (
            kind is type(None)
            or kind is bool
            or kind is int
            or kind is float
            or kind is str
            or kind is bytes
        ):
            return (value, kind, immutable_default)
        if kind is tuple or kind is list:
            children = tuple(default_snapshot(v, ancestors) for v in value)
            if kind is tuple and all(v[2] is immutable_default for v in children):
                return (value, kind, immutable_default)
            return (value, kind, children)
        if kind is dict:
            return (
                value,
                kind,
                tuple(
                    (default_snapshot(k, ancestors), default_snapshot(v, ancestors))
                    for k, v in value.items()
                ),
            )
        if kind is set or kind is frozenset:
            set_children = {id(v): default_snapshot(v, ancestors) for v in value}
            if kind is frozenset and all(
                v[2] is immutable_default for v in set_children.values()
            ):
                return (value, kind, immutable_default)
            return (value, kind, set_children)
        if kind is FunctionType:
            return (value, kind, value.__code__)
        # Unsupported defaults are opaque: never inspect a replacement's
        # attributes, repr, equality or hash. Exact type identity also detects
        # __class__ reassignment without invoking metaclass comparisons.
        return (value, kind, None)

    def default_unchanged(value, snapshot):
        original, kind, contents = snapshot
        if value is not original or type(value) is not kind:
            return False
        # Only exact immutable values/graphs can skip traversal. Mutable
        # containers, opaque types and function code are checked afresh below.
        if contents is immutable_default or contents is ancestor_default:
            return True
        if kind is tuple or kind is list:
            return len(value) == len(contents) and all(
                default_unchanged(current, saved)
                for current, saved in zip(value, contents)
            )
        if kind is dict:
            return len(value) == len(contents) and all(
                default_unchanged(key, saved_key)
                and default_unchanged(item, saved_item)
                for (key, item), (saved_key, saved_item) in zip(value.items(), contents)
            )
        if kind is set or kind is frozenset:
            return len(value) == len(contents) and all(
                (saved := contents.get(id(item))) is not None
                and default_unchanged(item, saved)
                for item in value
            )
        if kind is FunctionType:
            return value.__code__ is contents
        return True

    none_snapshot = default_snapshot(None)
    empty_defaults = (none_snapshot, none_snapshot)

    def defaults(function):
        if function.__defaults__ is None and function.__kwdefaults__ is None:
            return empty_defaults
        return (
            default_snapshot(function.__defaults__),
            default_snapshot(function.__kwdefaults__),
        )

    def defaults_unchanged(function, snapshot):
        positional, keywords = function.__defaults__, function.__kwdefaults__
        if positional is None and keywords is None:
            return snapshot is empty_defaults
        return default_unchanged(positional, snapshot[0]) and default_unchanged(
            keywords, snapshot[1]
        )

    def normalize(value):
        # Never ask an untrusted replacement export to describe itself. These
        # exact built-in containers have non-overridable function storage.
        if (
            type(value) is MethodType
            or type(value) is staticmethod
            or type(value) is classmethod
        ):
            return value.__func__
        return value

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
                or not defaults_unchanged(function, function_defaults[function])
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
        defined_classes: dict[type, None] = {}
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
                    if issubclass(type(member), property) or callable(member):
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
                    if issubclass(type(member), property):
                        for accessor in ("fget", "fset", "fdel"):
                            function = getattr(member, accessor)
                            if function is not None:
                                members.append(
                                    (member, accessor, function, function.__code__)
                                )
                defined_classes[value] = None
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
        # Each defining class is checked below through require_class, including
        # all inherited methods, property accessors and wrapper/default seals.
        # Keep only module bindings here instead of checking every class member
        # twice on every call. This is an index of immutable definitions, never
        # a cached successful validation: every request rechecks current source.
        registered[module] = tuple(row for row in bindings if row[0] is module)
        module_classes[module] = tuple(defined_classes)

    def require_bindings(bindings, *, class_snapshot=None):
        for owner, name, original, code in bindings:
            if class_snapshot is not None and owner is class_snapshot[0]:
                current = class_snapshot[1].get(name)
            else:
                current = normalize(
                    inspect.getattr_static(owner, name, None)
                    if inspect.isclass(owner)
                    else vars(owner).get(name)
                    if type(owner) is ModuleType
                    else getattr(owner, name, None)
                )
            if (
                current is not original
                or getattr(current, "__code__", None) is not code
                or (inspect.isfunction(current) and not function_unchanged(current))
            ):
                raise ReviewInputError("original module source changed: " + name)

    def static_members(cls):
        # For ordinary classes, getattr_static repeatedly walks the same MRO
        # and checks the immutable built-in type's dictionary descriptor for
        # every attribute. Read those current namespaces once instead. Exact
        # type/key guards exclude user-controlled lookup/equality/hash hooks;
        # unusual metaclasses and namespaces retain the original static reader.
        # This is a fresh view, never a cached successful source validation.
        if type(cls) is type:
            namespaces = []
            for base in type.__getattribute__(cls, "__mro__"):
                if type(base) is not type:
                    break
                namespace = vars(base)
                if any(type(name) is not str for name in namespace):
                    break
                namespaces.append(namespace)
            else:
                merged = {}
                for namespace in reversed(namespaces):
                    merged.update(namespace)
                # With the exact built-in metaclass, type.__dir__ enumerates
                # these same namespaces again and sorts their names. Neither
                # a second traversal nor sorting contributes source evidence.
                return {name: normalize(member) for name, member in merged.items()}
        return {name: normalize(inspect.getattr_static(cls, name)) for name in dir(cls)}

    def require_class(cls):
        original = classes.get(cls)
        if original is None:
            raise ReviewInputError("original source class is unavailable")
        module, name, namespace, executable, bindings, _methods, slots = original
        slot_values = slots[0]
        if type(module) is not ModuleType:
            raise ReviewInputError("original class source changed")
        module_name = vars(module).get("__name__")
        if (
            type(module_name) is not str
            or sys.modules.get(module_name) is not module
            or vars(module).get(name) is not cls
            or frozenset(vars(cls)) != namespace
            or vars(cls).get("__slotnames__") is not slots[0]
            or type(slot_values) is not list
            or any(type(slot) is not str for slot in slot_values)
            or tuple(slot_values) != slots[1]
        ):
            raise ReviewInputError("original class source changed")
        # One fresh static snapshot serves both executable membership and
        # binding checks. It is discarded after this validation; no method or
        # successful source check is cached between requests. Preserve the
        # original early metadata refusals before enumerating any members.
        members = static_members(cls)
        if (
            frozenset(
                name
                for name, member in members.items()
                if callable(member) or issubclass(type(member), property)
            )
            != executable
        ):
            raise ReviewInputError("original class source changed")
        require_bindings(bindings, class_snapshot=(cls, members))

    def require_module_seal(module):
        if type(module) is not ModuleType:
            raise ReviewInputError("original source module seal is unavailable")
        bindings = registered.get(module)
        module_name = vars(module).get("__name__")
        if (
            bindings is None
            or type(module_name) is not str
            or sys.modules.get(module_name) is not module
        ):
            raise ReviewInputError("original source module seal is unavailable")
        require_bindings(bindings)
        for cls in module_classes[module]:
            require_class(cls)

    def require_original_class_identity(instance, *, module, name):
        """Check defining class identity only, never executable source authority.

        Negative optional-capacity classification may support public generic
        method overrides. Guarded consumers must still require full source.
        """
        cls = type(instance)
        if (
            type(cls) is not type
            or type(module) is not ModuleType
            or type(name) is not str
        ):
            raise ReviewInputError("original source class identity is unavailable")
        original = classes.get(cls)
        if (
            original is None
            or original[0] is not module
            or original[1] != name
            or type(vars(module).get("__name__")) is not str
            or sys.modules.get(vars(module)["__name__"]) is not module
            or vars(module).get(name) is not cls
        ):
            raise ReviewInputError("original source class identity changed")

    def require_original_class_export(*, module, name):
        """Return registered export identity only; never construct an instance."""
        if type(module) is not ModuleType or type(name) is not str:
            raise ReviewInputError("original source class identity is unavailable")
        namespace = vars(module)
        cls = namespace.get(name)
        if type(cls) is not type:
            raise ReviewInputError("original source class identity is unavailable")
        original = classes.get(cls)
        if (
            original is None
            or original[0] is not module
            or original[1] != name
            or type(namespace.get("__name__")) is not str
            or sys.modules.get(namespace["__name__"]) is not module
            or namespace.get(name) is not cls
        ):
            raise ReviewInputError("original source class identity changed")
        return cls

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

    return (
        seal_module,
        require_module_seal,
        require_instance_source,
        require_original_class_identity,
        require_original_class_export,
    )


(
    seal_module,
    require_module_seal,
    require_instance_source,
    require_original_class_identity,
    require_original_class_export,
) = _build_registry()
del _build_registry
seal_module()
