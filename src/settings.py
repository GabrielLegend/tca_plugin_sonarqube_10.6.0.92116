#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================


import os
import sys


VERSION = "1.0.0"


PLATFORMS = {
    "linux2": "linux",
    "linux": "linux",
    "win32": "windows",
    "darwin": "mac",
}


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
TOOL_DIR = os.path.join(ROOT_DIR, "tools")
SONARQUBE_HOME = os.path.join(TOOL_DIR, "common", "sonarqube-10.6.0.92116")
SONAR_SCANNER_HOME = os.path.join(TOOL_DIR, PLATFORMS[sys.platform], "sonar-scanner-6.1.0.4477")
SQ_JDK_HOME = os.path.join(SONARQUBE_HOME, "jre")


# ========================
# SonarQube settings
# ========================

SQ_LOCAL_USER = {
    "url": "http://localhost",
    "port": 9000,
    "base_path": "",
    "username": "admin",
    "password": "admin",
    "projectKey": "test",
}


SQ_COMMON_USER = None
