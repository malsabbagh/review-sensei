"""Test oracle matching GitHub's rejection of duplicate workflow mapping keys."""

import yaml


class _UniqueKeysLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise yaml.constructor.ConstructorError(
                    "while constructing a workflow mapping",
                    node.start_mark,
                    f"duplicate mapping key: {key}",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_workflow(text):
    return yaml.load(text, Loader=_UniqueKeysLoader)
