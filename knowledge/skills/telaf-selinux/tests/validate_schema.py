"""Tiny validator for precisely the JSON Schema keywords used by this skill.

Not a general JSON Schema implementation. Unknown keywords fail closed.
"""
import re


def validate(value, schema, root):
    supported = {'$schema', 'title', '$defs', '$ref', 'type', 'required', 'properties',
                 'additionalProperties', 'items', 'enum', 'minLength', 'minItems', 'pattern'}
    if set(schema) - supported:
        raise ValueError('Unsupported schema keyword')
    if '$ref' in schema:
        target = root
        for key in schema['$ref'].removeprefix('#/').split('/'):
            target = target[key]
        validate(value, target, root)
        return
    kinds = schema.get('type', [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'string': isinstance(value, str), 'boolean': isinstance(value, bool),
               'integer': isinstance(value, int) and not isinstance(value, bool)}
    if kinds and not any(matches.get(kind, False) for kind in kinds):
        raise ValueError('Wrong type')
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        raise ValueError('Invalid enum')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            raise ValueError('String too short')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise ValueError('Pattern mismatch')
    if isinstance(value, dict):
        if set(schema.get('required', [])) - value.keys():
            raise ValueError('Missing required keys')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and value.keys() - properties.keys():
            raise ValueError('Unknown keys')
        for key in value.keys() & properties.keys():
            validate(value[key], properties[key], root)
    if isinstance(value, list) and len(value) < schema.get('minItems', 0):
        raise ValueError('Array too short')
    if isinstance(value, list) and 'items' in schema:
        for entry in value:
            validate(entry, schema['items'], root)
