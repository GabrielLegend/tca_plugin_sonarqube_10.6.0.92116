#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================


import os
import re
import json

from tomd import Tomd

import settings
from util.api import SQAPIHandler


# INFO MINOR -> info
# MAJOR -> warning
# CRITICAL BLOCKER -> error
SEVERITY_MAP = {
    "INFO": "info",
    "MINOR": "info",
    "MAJOR": "warning",
    "CRITICAL": "error",
    "BLOCKER": "error",
}

# CODE_SMELL
# BUG
# VULNERABILITY
# SECURITY_HOTSPOT
TYPE_MAP = {
    "CODE_SMELL": "convention",
    "BUG": "correctness",
    "VULNERABILITY": "security",
    "SECURITY_HOTSPOT": "security",
}

LANGUAGE_MAP = {
    # language列表为空，表示通用语言
    "azureresourcemanager": None,
    "cloudformation": None,
    "java": "java",
    "docker": None,
    "cs": "cs",
    "web": "html",
    "py": "python",
    "css": "css",
    "flex": "flex",
    "go": "Go",
    "js": "js",
    "kotlin": "kotlin",
    "kubernetes": None,
    "php": "php",
    "ruby": "ruby",
    "scala": "scala",
    "secrets": None,
    "terraform": None,
    "text": None,
    "ts": "ts",
    "vbnet": "visualbasic",
    "xml": "xml",
}


def display_name(name):
    pieces = re.split(":|-", name)
    return "".join([piece.capitalize() for piece in pieces])


if __name__ == "__main__":
    handler = SQAPIHandler(
        user=settings.SQ_LOCAL_USER["username"],
        password=settings.SQ_LOCAL_USER["password"],
    )

    for key, langs in {
        "sq": [
            "azureresourcemanager",
            "cloudformation",
            "css",
            "docker",
            "flex",
            "go",
            "web",
            "js",
            "kotlin",
            "kubernetes",
            "php",
            "py",
            "ruby",
            "scala",
            "secrets",
            "terraform",
            "text",
            "ts",
            "xml",
        ],
        "sq_cs": ["cs"],
        "sq_visualbasic": ["vbnet"],
        "sq_java": ["java"],
    }.items():
        # print(lang)
        rules = list()
        for lang in langs:
            for rule in handler.get_rules(
                active_only=True,
                languages=lang,
                f="name,severity,lang,mdDesc",
            ):
                # print(json.dumps(rule))
                rules.append({
                    "real_name": rule["key"],
                    # 64 char
                    "display_name": display_name(rule["key"]),
                    "severity": SEVERITY_MAP[rule["severity"]],
                    "category": TYPE_MAP[rule["type"]],
                    "rule_title": rule["name"],
                    "rule_params": None,
                    "custom": False,
                    "languages": [LANGUAGE_MAP[rule["lang"]]] if LANGUAGE_MAP[rule["lang"]] is not None else [],
                    "solution": None,
                    "owner": None,
                    "labels": [],
                    "description": Tomd(rule["mdDesc"]).markdown if rule["mdDesc"].startswith("<p>") else rule["mdDesc"],
                    "disable": False
                })

        f = open(os.path.join(os.path.dirname(settings.TOOL_DIR), "config", f"{key}.json"))
        config = json.load(f)
        f.close()
        config[0]["checkrule_set"] = rules

        out_dir = os.path.join(os.path.dirname(settings.TOOL_DIR), "config-new")
        if not os.path.exists(out_dir):
            os.mkdir(out_dir)
        f = open(os.path.join(out_dir, f"{key}.json"), "w")
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.close()
    
