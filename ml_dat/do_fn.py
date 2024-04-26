#!/usr/bin/env python3

"""
The 'do' module provides a way to dynamically load and execute python code.

See DoManager class for details.

"""

import os
import sys
import copy
import json
from importlib import import_module

import yaml
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Type, Union, Any, Dict, Callable

from ml_dat.inst import Inst

# The loadable "do" fns, scripts, configs, and methods are in the do_folder
_DO_EXTENSIONS = [".json", ".yaml", ".py"]
_DO_ERROR_FLAG = tuple("multiple loadable modules have this same name")
_DO_NULL = tuple(["-no-value-"])

_MAIN_BASE = "main.base"
_MAIN_DO = "main.do"              # the main fn to execute
_MAIN_ARGS = "main.args"          # prefix args for the main.do method
_MAIN_KWARGS = "main.kwargs"      # default kwargs for the main.do method

Spec = Dict[str, Any]


class DoManager(object):
    """'Do' maps dotted strings to python objects dynamically loaded from .py files.

    API:
      .load(dotted_name) ............. # Loads and returns the indexed object
      (do_spec, *args, **kwargs)  .... # Calls the object loaded from the spec name
      .register_value(name, value) ... # Defines value to be returned by load
      .register_module(base, path) ... # Specifies path of module to be loaded


    DOTTED-NAMES
    - The first part of the dotted string is called the "base" and it indicates the
        python module, JSON, or YAML file where the value is loaded from.
    - The second part, called the attribute, indicates the attribute of the module
        where the object is found.
    - Any remaining dotted-names are used to recursively index within the object.
    - As a special case if the dotted-name is a single part, and the module

    DO_FOLDER
    - Like GIT the 'do' module searches CWD and all its parent folders for the
      '.datconfig' file. If it is found, it expects it to contain a JSON object.
    - If it contains the 'do_folder' key, its value specifies the relative path from
      the datconfig file to the "do folder" where the loadable python objects are found.
    - Otherwise, the do folder is assumed to be a folder named 'do' in the CWD.
    - Either way the do folder is scanned as the filenames (but not their paths) are
      used as the "base" part of the dotted names.

    SPEC EXPANSION
    - Spec expansion is the process of recursively loading and merging a spec dict:
    - If a spec has a "main.base" key, then it is loaded and merged with the spec.
        - This process is repeated until no more "main.base" keys are found.

    """
    do_folder: str  #
    module_index: Dict[str, Union[str, ModuleType]]    # path to module or module itself
    do_fns: Dict[str, Dict[str, Callable]]             # externally defined fns
    registered_values: Union[None, Dict[str, Any]]     # values to be returned by load

    def __init__(self, *, do_folder=None):
        self.set_do_folder(do_folder)

    def __call__(self, do_spec: Union[Spec, str], *args, **kwargs) -> Any:
        """Loads and executes a 'do-method'.

        A "do function" or "do method" is a configurable dynamically loadable function
        call. Each 'do' accepts a multi-level configuration dict as its first
        argument followed by any number of additional fixed and keyword arguments.

        If "do_spec" is a:
        - STRING: then 'do.load' is used to get the value to use in its place
        - DICT: then its 'main.do' sub-key specifies the function to execute
          If it is a string then, get_loadable is used to get fn to execute
          Otherwise it is directly called.
        - CALLABLE: then it is directly called with the specified args and kwargs

        The function found is called with the spec dict found as its first
        argument, then specified args and kwargs after that.
        """
        obj = self.load(do_spec) if isinstance(do_spec, str) else do_spec
        if callable(obj):
            result = obj(*args, **kwargs)
            return result
        try:
            obj = self.expand_spec(obj, context=do_spec)
        except ValueError as e:
            raise Exception(F"DO - Error during expansion of {do_spec!r}: {e}")
        try:
            fn_spec = Inst.get(obj, _MAIN_DO)
        except ValueError:
            raise Exception(F"DO: Error {do_spec!r} is not a callable or config")
        if isinstance(fn_spec, str):
            fn_spec = self.load(fn_spec)
        if not callable(fn_spec):
            raise Exception(F"'{_MAIN_DO}' in {do_spec!r} of type {type(fn_spec)} "
                            + "is not callable")
        result = fn_spec(obj, *args, **kwargs)
        return result

    def set_do_folder(self, do_folder):
        """Sets the folder where the loadable python objects are found, and clears all
        cached modules and values."""
        self.do_folder = do_folder
        self.module_index = _build_loadables_index(do_folder)
        self.registered_values = None

    def register_module(self, base: str, module_spec: Union[str, ModuleType], *,
                        allow_redefine=False):
        """Specifies the module associated with a dotted name prefix.

        The module can be specified using
        - The full path to the python source (which will be loaded separately from the
          import system.),
        - The module's import name, or
        - by providing the already loaded module object."""
        if not allow_redefine and base in self.module_index and \
                True:
            raise Exception(F"Base {base!r} is already defined")
        self.module_index[base] = module_spec

    def register_value(self, dotted_name: str, value: Any):
        """Defines a value to be returned by 'load' for the given dotted name."""
        if self.registered_values is None:
            self.registered_values = {}
        self.registered_values[dotted_name] = value
        # print(f"Registered {dotted_name} as {value} in {self}")

    def get_base_object(self, base: str) -> Any:
        """Returns the module associated with the given base name."""
        if base not in self.module_index:
            return None
        elif isinstance(self.module_index[base], str):
            return _load_base_entity(self.module_index[base])
        else:
            return self.module_index[base]

    def merge_configs(self, base, override):
        """Recursively merges the 'override' dict trees over 'base' tree of dicts."""
        if isinstance(override, dict) and isinstance(base, dict):
            merge = dict(base)
            for k, v in override.items():
                merge[k] = self.merge_configs(merge.get(k), v)
            # if BASE in base:
            #     merge[BASE] = base[BASE]
            return merge
        elif override:
            return override
        else:
            return base

    def expand_spec(self, spec: Union[Spec | str], context: str = None) -> Spec:
        """Expands a spec by recursively loading and expanding its 'main.base' spec,
        and then merging its keys as an override to the expanded base."""
        if isinstance(spec, str):
            spec = self.load(spec, context=context)
            spec = copy.deepcopy(spec)
        if base := Inst.get(spec, _MAIN_BASE):
            sub_spec = self.expand_spec(base, context=context)
            return self.merge_configs(sub_spec, spec)
        else:
            return spec

    def load(self, dotted_name: str, *, default: Any = _DO_NULL, kind: Type = None,
             context=None) -> Any:
        """Uses 'dotted_name' do find a "loadable" python object, and then dynamically
        load this object from within the loadables folder.

        - If NAME contains a dot ("."), then the first part is called the
          FILENAME, and the second part is called the PART-NAME. If there is
          no dot, then "NAME" is used twice for both FILENAME and PART-NAME.

        - If FILENAME.* is found multiple times in the loadable's folder, then
          an error is generated indicating the ambiguity in loading this name.

        - If FILENAME.json or FILENAME.yaml is found, then it is loaded, and its
          parsed contents are returned.  (PART-NAME is ignored)
        """

        ctx = "" if context is None else F" {context}"
        parts = dotted_name.split(".")
        prefix = parts[0]
        obj = self.module_index.get(prefix)
        if isinstance(obj, str):
            self.module_index[prefix] = obj = _load_base_entity(obj)
        if self.registered_values and _DO_NULL != \
                (value := self.registered_values.get(dotted_name, _DO_NULL)):
            return value
        elif obj is None and default is _DO_NULL:
            raise Exception(
                    f"DO: Module {prefix!r} not found under {self.do_folder!r}{ctx}")
        elif obj is None:
            return default
        elif obj == _DO_ERROR_FLAG:
            raise Exception(F"DO: Module {prefix!r} is defined multiple times.{ctx}")

        if isinstance(obj, ModuleType):
            idx = 1 if len(parts) > 1 else 0
            result = getattr(obj, parts[idx]) if hasattr(obj, parts[idx]) else None
            if len(parts) > 2:
                result = Inst.get(result, parts[2:])
        elif len(parts) == 1:
            result = obj
        elif isinstance(obj, dict):
            result = Inst.get(obj, parts[1:])
        else:
            raise Exception(F"DO: Illegal value {obj!r} for {dotted_name}{ctx}")

        if result is None and default is not _DO_NULL:
            return default
        if result is None:
            if obj is None:
                raise Exception(F"DO: Module {prefix!r} not found{ctx}")
            else:
                raise Exception(
                    F"DO: Value {dotted_name[len(prefix)+1:]!r} is missing from {obj}")
        if kind and not isinstance(result, kind):
            raise Exception(F"DO: Expected {dotted_name!r} of type {kind} " +
                            F"but found {result!r}")
        return result


def _load_base_entity(source_spec: str) -> Union[ModuleType, Spec]:
    ext = os.path.splitext(source_spec)[1]
    if ext == ".py" or "/" not in source_spec:
        return _load_module(source_spec)
    elif ext == ".json":    # os.path.exists(name := F"{path_base}.json"):
        with open(source_spec, 'r') as f:
            try:
                return json.load(f)
            except Exception as e:
                raise Exception(F"While parsing {source_spec}, {e}")
    elif ext == ".yaml":     # os.path.exists(name := F"{path}.yaml"):
        with open(source_spec, 'r') as f:
            return yaml.safe_load(f)
    else:
        raise Exception(F"DO: Unsupported file type {source_spec}")


def _load_module(module_spec: str) -> ModuleType:
    if "/" not in module_spec:
        try:
            return import_module(module_spec)
        except ModuleNotFoundError:
            raise Exception(F"DO.LOAD: Module {module_spec!r} not found.")
    assert isinstance(module_spec, object)
    if not os.path.exists(module_spec):
        raise Exception(F"Missing module file: {module_spec} ...")
    spec = importlib.util.spec_from_file_location("module_name", module_spec)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_loadables_index(do_folder: str) -> Dict:
    global _DO_EXTENSIONS
    result = {}
    if not do_folder or not os.path.exists(do_folder):
        return result
    for path in Path(do_folder).rglob('*'):
        base, ext = os.path.splitext(os.path.basename(path))
        if not path.is_file() or ext not in _DO_EXTENSIONS or \
                base == '__init__':
            continue
        elif base in result:
            print("WARNING: loadable at" +
                  F"{result[base]} conflicts with {path}")
            result[base] = _DO_ERROR_FLAG
        else:
            result[base] = str(path)
    return result


USAGE = """
SYNOPSIS
    do CMD_NAME FIXED_ARGS ... KEYWORD_ARG ...
    do KEY_WORD_ARGS  ...  CMD_NAME FIXED_ARGS ...

    do --usage
    do --get DOTTED.KEY
    do --set DOTTED.KEY=VALUE
    do --sets "DOTTED.KEY1=VALUE1, DOTTED.KEY2=VALUE2"

DESCRIPTION
    Executes the do command named by CMD_NAME.
    
    --usage     Prints the command-specific usage info if it exists
    
    --USAGE     Prints this usage message
    
    --print     Prints the python do call with args, but does not call it.
    
    --get DOTTED.NAME
                Expands the config for a command and returns an arg from it
    
    --set DOTTED.NAME=VALUE
    --sets DOTTED.NAME1=VALUE1,DOTTED.NAME2=VALUE2,...
                Expands the config for a command and updates the indicated
                config parameters before invoking the indicated command

NOTES
    Per standard UNIX 'getopt' parameter parsing two dashes ("--")
    can be used to terminate keyword arguments and cause all remaining 
    arguments to be treated as fixed parameters even when those parameters
    begin with "-" in a way that could be confused as additional keywords

    Unlike most UNIX parameters each single dash ("-") keywords cannot
    be concatenated.  So "do -a -b foo" cannot be shorted to "do -ab foo"
    
    All keyword arguments can be either flags or keywords with arguments, 
    thus "--" must be added in some cases to avoid treating a fixed arg
    as the value associated with a keyword flag.
    

EXAMPLES

    do --show balls,hoops viz
"""


def do_argv(argv):
    """
    --usage will Look up "CMD_NAME.usage" to see if usage was specified, else it
    looks at the "usage" key in config for usage, else print default usage
    """
    from . import do
    overrides, args, kwargs = _parse_argv(argv[1:])
    # print(F"DO  args={args!r}   kwargs={kwargs!r}")
    if "usage" in kwargs or (not args and not kwargs):
        print(USAGE)
        return
    elif len(args) == 0 and "usage" not in kwargs:
        print("Error: No do-command specified.")
        return
    cmd = do.load(args[0])
    spec = do.expand_spec(cmd) if hasattr(cmd, "__getitem__") else {}
    spec = do.merge_configs(spec, overrides)
    # for keys, value in overrides:
    #     Inst.set(spec, keys, value)
    if "usage" in kwargs:
        try:
            usage = do.load(args[0].split(".")[0] + ".usage")
        except IOError:
            usage = spec.get("usage") or USAGE
        print(usage)
        return
    elif "print" in kwargs:
        del kwargs["print"]
        args = [repr(a) for a in args]
        kwargs = [F"{k}={repr(v)}" for k, v in kwargs.items()]
        print(F"  do({', '.join(args + kwargs)})")
        return
    elif spec:
        args_ = Inst.get(spec, _MAIN_ARGS) or []
        kwargs_ = Inst.get(spec, _MAIN_KWARGS) or {}
        kwargs_.update(kwargs)
        result = do(spec, *args_ + args[1:], **kwargs_)
    elif not callable(cmd):
        print(cmd)
        return
    elif overrides:
        print("Error: Cannot specify --set or --sets on a do w/o a config")
        return
    else:
        result = do(*args, **kwargs)
    if result is not None:
        print(result)


def _parse_argv(argv):
    overrides, args, kwargs, i, argv = {}, [], {}, 0, argv + ["--end-of-args"]
    while i < len(argv) - 1:
        arg = argv[i]
        flag = _get_flag(arg)
        if arg == "--":
            args += argv[i+1:-1]
            break
        elif arg == "--json":
            try:
                Inst.set(overrides, argv[i+1], json.loads(argv[i+2]))
                # overrides.append((argv[i+1], json.loads(argv[i+2])))
            except json.decoder.JSONDecodeError:
                print(F"Illegal JSON: {argv[i+2]}")
            i += 2
        elif arg == '--set':
            Inst.set(overrides, argv[i+1], argv[i+2])
            # overrides.append((argv[i+1], argv[i+2]))
            i += 2
        elif arg == "--sets":
            Inst.sets(overrides, *argv[i+1].split(","))
            # overrides += [part.strip().split("=") for part in argv[i+1].split(",")]
            # assignments += map(str.strip, argv[i+1].split(','))
            i += 1
        elif not flag:
            args.append(arg)
        elif _get_flag(argv[i + 1]):
            kwargs[flag] = True
        else:
            kwargs[flag] = argv[i + 1]
            i += 1
        i += 1
    return overrides, args, kwargs


def _get_flag(arg):
    if not all(c.isalnum() or c == '-' for c in arg):
        return None
    elif arg == "--":
        return arg
    elif arg.startswith("--"):
        return arg[2:].replace("-", "_")
    elif arg.startswith("-") and len(arg) == 2:
        return arg[1]
    else:
        return None


import ml_dat
# if not hasattr(ml_dat, "dat_config"):
#     ml_dat.dat_config = ml_dat.ml_dat_config.DatConfig()
ml_dat.DoManager = DoManager
ml_dat.do = ml_dat.dodo = DoManager(do_folder=ml_dat.ml_dat_config.dat_config.do_folder)
ml_dat.argv = do_argv


if __name__ == '__main__':
    # do_argv([None, "dt.list"])
    # do_argv([None, "cmdln_example", "--show-spec"])
    # do_argv([None, "my_letters", "--set", "main.title", "Re-configured letterator",
    # "--json", "rules", '[[2, "my_letters.triple_it"]]'])
    # do_argv([None, "my_letters", "--sets", "main.title=QuickChart,start=100,end=110"])
    do_argv(sys.argv)
