"""One strict JSON boundary for HTTP requests and device frames."""
import json
import math


def _constant(value):
    raise ValueError("non-finite JSON number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def loads(data):
    return json.loads(data, parse_constant=_constant, parse_float=_float, object_pairs_hook=_object)
