#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================


import os
import json

from util.base import COMMON_SONAR_LANGS, Sonar as SonarQubeUtil


class SonarQube(object):
    def run(self):
        """
        :return:
        """
        sonar_scanner = SonarQubeUtil()
        build_cwd = os.environ.get("BUILD_CWD", None)
        build_cwd = os.path.join(sonar_scanner.source_dir, build_cwd) if build_cwd else sonar_scanner.source_dir

        # sonar_scanner.pre_cmd(build_cwd)
        issues = sonar_scanner.scan_proj(
            sonar_scanner.scan_not_build_proj,
            languages=",".join(COMMON_SONAR_LANGS),
            build_cwd=build_cwd,
        )

        with open("result.json", "w") as fp:
            json.dump(issues, fp, indent=2)


tool = SonarQube


if __name__ == "__main__":
    print("-- start run tool ...")
    tool().run()
    print("-- end ...")
