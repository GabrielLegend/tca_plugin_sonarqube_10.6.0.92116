#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================


import os
import json

from util.base import Sonar


if __name__ == "__main__":
    print("check tool usable ...")
    is_usable = Sonar.check_usable()
    result_path = "check_result.json"
    if os.path.exists(result_path):
        os.remove(result_path)
    with open(result_path, "w") as fp:
        data = {"usable": is_usable}
        json.dump(data, fp)
