#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================

"""
SonarQube for Java
"""

import os
import json

from util.base import Sonar as SonarQubeUtil


class SonarQubeJava(object):
    def run(self):
        """
        :return:
        """
        print("当前执行模式BUILD_TYPE: %s" % build_type)
        sonar_scanner = SonarQubeUtil()
        envs = os.environ
        build_cmd = sonar_scanner.params.get("build_cmd", None)
        build_cwd = envs.get("BUILD_CWD", None)
        build_cwd = os.path.join(sonar_scanner.source_dir, build_cwd) if build_cwd else sonar_scanner.source_dir
        build_type = envs.get("SONAR_BUILD_TYPE", "no_build").lower()
        
        sonar_scanner.pre_cmd(build_cwd)
        issues = sonar_scanner.scan_proj(
            sonar_scanner.scan_java_proj,
            languages="java",
            build_type=build_type,
            build_cwd=build_cwd,
            build_cmd=build_cmd,
        )

        with open("result.json", "w") as fp:
            json.dump(issues, fp, indent=2)


tool = SonarQubeJava


if __name__ == "__main__":
    print("-- start run tool ...")
    tool().run()
    print("-- end ...")
