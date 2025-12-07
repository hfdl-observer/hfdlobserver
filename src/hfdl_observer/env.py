# hfdl_observer/env.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import os
import pathlib
import shutil

base_path = pathlib.Path(os.getcwd())


def as_path(path_str: str, make_absolute: bool = True) -> pathlib.Path:
    path = pathlib.Path(os.path.expanduser(os.path.expandvars(path_str)))
    if path.is_absolute() or not make_absolute:
        return path
    else:
        return base_path / path


def as_executable_path(path_str: str) -> pathlib.Path:
    # find the executable.
    for absolute in [False, True]:
        executable_path = as_path(path_str, make_absolute=absolute)
        if shutil.which(executable_path):
            return executable_path
    raise FileNotFoundError(path_str)
