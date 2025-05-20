#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================

"""
SonarQube for C#
"""

import os
import json

from util.base import Sonar as SonarQubeUtil


class SonarQubeCs(object):
    def run(self):
        """
        :return:
        """
        sonar_scanner = SonarQubeUtil()
        build_cmd = sonar_scanner.params["build_cmd"]
        build_cwd = os.environ.get("BUILD_CWD", None)
        build_cwd = os.path.join(sonar_scanner.source_dir, build_cwd) if build_cwd else sonar_scanner.source_dir

        sonar_scanner.pre_cmd(build_cwd)
        issues = sonar_scanner.scan_proj(
            sonar_scanner.scan_cs_vb_proj, languages="cs", build_cmd=build_cmd, build_cwd=build_cwd
        )

        with open("result.json", "w") as fp:
            json.dump(issues, fp, indent=2)


tool = SonarQubeCs


if __name__ == "__main__":
    print("-- start run tool ...")
    tool().run()
    print("-- end ...")
