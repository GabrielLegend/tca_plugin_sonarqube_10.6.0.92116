#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================

import re
import os
import sys
import json
import psutil
import platform
import stat
from subprocess import Popen as p, PIPE as pi, STDOUT as sout
from threading import Thread as t

import settings

# Sonar技术负债默认配置
# 默认设置为9
SONAR_DEVCOST = 9
SONAR_DEBT_RATINGGRID = "0.05,0.1,0.2,0.5"

# SonarQube的信息
SQ_LOCAL_USER = getattr(settings, "SQ_LOCAL_USER", None)
SQ_COMMON_USER = getattr(settings, "SQ_COMMON_USER", None)

# 连接模式
LOCAL_MODEL = "LOCAL"
# 免费版远程服务
COMMON_MODEL = "COMMON"

COMMON_SONAR_LANGS = [
    "azureresourcemanager",
    "cloudformation",
    "cs",
    "css",
    "docker",
    "flex",
    "go",
    "web",
    "java",
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
    "vbnet",
    "xml",
]


def chmod_ancestor_dir(path, mode):
    """
    向上递归设置祖先目录权限
    :param path:
    :param mode:
    :return:
    """
    father_dir = os.path.abspath(path)
    # windows下的话，最后是D:
    while father_dir != "/":
        os.chmod(father_dir, mode)
        father_dir = os.path.dirname(father_dir)


def change_to_win_cmd(cmd):
    """
    修改Sonar在Mac/linux命令为win下的命令
    :param cmd:
    :return:
    """
    if sys.platform != "win32":
        return cmd
    result = list()
    for c in cmd:
        if c.startswith("-D"):
            result.append('-D"' + c[2:] + '"')
        else:
            result.append(c)
    return result

def kill_proc_famliy(pid):
    try:
        task_proc = psutil.Process(pid)
        children = task_proc.children(recursive=True)
        print("[info] kill process: %s" % task_proc)
        task_proc.terminate()
        print("[info] kill children processes: %s" % children)
        for child in children:
            try:
                child.kill()
            except Exception as err:
                print("[error] kill child proc failed: %s" % err)
        gone, still_alive = psutil.wait_procs(children, timeout=5)
        for child in still_alive:
            try:
                child.kill()
            except Exception as err:
                print("[error] kill child proc failed: %s" % err)
    except psutil.NoSuchProcess as err:
        print("[warning] process is already terminated: %s" % err)
    except Exception as err:
        print("[error] kill task failed: %s" % err)


def generate_shell_file(cmd, shell_name="build"):
    work_dir = os.getcwd()
    if platform.system() == "Windows":
        file_name = f"{shell_name}.bat"
    else:
        file_name = f"{shell_name}.sh"
    shell_filepath = os.path.join(work_dir, file_name)
    shell_filepath = os.path.abspath(shell_filepath.strip()).replace("\\", "/").rstrip("/")
    with open(shell_filepath, "w") as wf:
        wf.write(cmd)
    os.chmod(shell_filepath, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)

    print("[info] Cmd:\n%s" % cmd)
    print("[info] Generated shell file: %s" % shell_filepath)

    if platform.system() == "Windows":
        return shell_filepath
    else:
        return "bash %s" % shell_filepath


def decode(line):
    try:
        # UTF-8
        line = line.decode()
    except UnicodeDecodeError:
        line = line.decode(encoding="gbk")
    return line


class Process(object):
    def __init__(self, command, cwd=None, out=None, err=None, shell=False):
        # print(" ".join(command))
        if shell : command = " ".join(command)
        self.p = p(command, cwd=cwd, stdout=pi, stderr=pi, shell=shell)
        if out:
            out_t = t(target=self.do, args=(self.p.stdout, out))
            out_t.start()
        if err:
            err_t = t(target=self.do, args=(self.p.stderr, err))
            err_t.start()

    def wait(self):
        self.p.wait()

    def do(self, pipe, callback=None):
        while self.p.poll() is None:
            out = pipe.readline()
            out = bytes.decode(out)
            if out:
                callback(out)
        out = pipe.read()
        if out:
            callback(out)


class SQBase(object):
    def __init__(self) -> None:
        self.init_env()

        task_request_file = os.environ.get("TASK_REQUEST")
        print("[debug] task_request_file: %s" % task_request_file)
        with open(task_request_file, "r") as rf:
            task_request = json.load(rf)
        task_params = task_request["task_params"]
        task_params["task_dir"] = os.path.abspath(task_request["task_dir"])
        self.params = task_params
        self.source_dir = os.environ.get("SOURCE_DIR", None)
        # ------------------------------------------------------------------ #
        # 获取需要扫描的文件列表
        # 此处获取到的文件列表,已经根据项目配置的过滤路径过滤
        # 增量扫描时，从SCAN_FILES获取到的文件列表与从DIFF_FILES获取到的相同
        # ------------------------------------------------------------------ #
        self.scan_files = []
        scan_files_env = os.getenv("SCAN_FILES")
        if scan_files_env and os.path.exists(scan_files_env):
            with open(scan_files_env, "r") as rf:
                self.scan_files = json.load(rf)
                print("[debug] files to scan: %s" % len(self.scan_files))

        # ------------------------------------------------------------------ #
        # 增量扫描时,可以通过环境变量获取到diff文件列表,只扫描diff文件,减少耗时
        # 此处获取到的diff文件列表,已经根据项目配置的过滤路径过滤
        # ------------------------------------------------------------------ #
        # 从 DIFF_FILES 环境变量中获取增量文件列表存放的文件(全量扫描时没有这个环境变量)
        self.diff_files = []
        diff_file_env = os.environ.get("DIFF_FILES")
        if diff_file_env and os.path.exists(diff_file_env):  # 如果存在 DIFF_FILES, 说明是增量扫描, 直接获取增量文件列表
            with open(diff_file_env, "r") as rf:
                self.diff_files = json.load(rf)
                print("[debug] get diff files: %s" % self.diff_files)

    @staticmethod
    def init_env():
        # tool_dir = settings.TOOL_DIR
        os.environ["SONAR_SCANNER_HOME"] = settings.SONAR_SCANNER_HOME
        os.environ["SQ_JDK_HOME"] = settings.SQ_JDK_HOME
        os.environ["JAVA_HOME"] = settings.SQ_JDK_HOME
        os.environ["SONARQUBE_HOME"] = settings.SONARQUBE_HOME
        os.environ["PATH"] = os.pathsep.join(
            [
                os.path.join(os.environ["SQ_JDK_HOME"], "bin"),
                os.path.join(os.environ["SONAR_SCANNER_HOME"], "bin"),
                os.environ["PATH"],
            ]
        )


class JVMProxy(object):
    
    @staticmethod
    def get_proxy_args():
        """
        解析代理环境变量
        """
        proxy_args = []
        pattern = re.compile(r"https?://((.*):(.*)@)?(.*):(\d+)")

        # 解析http_proxy
        http_proxy = os.environ.get("HTTP_PROXY")
        if http_proxy is None:
            http_proxy = os.environ.get("http_proxy")
        if http_proxy is not None:
            match = pattern.match(http_proxy)
            if match:
                # print(f"match.groups(): {match.group(0)}")
                # 有账号密码
                if match.group(1):
                    proxy_args.append(f"-Dhttp.proxyUser={match.group(2)}")
                    proxy_args.append(f"-Dhttp.proxyPassword={match.group(3)}")
                proxy_args.append(f"-Dhttp.proxyHost={match.group(4)}")
                proxy_args.append(f"-Dhttp.proxyPort={match.group(5)}")

        https_proxy = os.environ.get("HTTPS_PROXY")
        if https_proxy is None:
            https_proxy = os.environ.get("https_proxy")
        if https_proxy is not None:
            match = pattern.match(https_proxy)
            if match:
                # 有账号密码
                if match.group(1):
                    proxy_args.append(f"-Dhttps.proxyUser={match.group(2)}")
                    proxy_args.append(f"-Dhttps.proxyPassword={match.group(3)}")
                proxy_args.append(f"-Dhttps.proxyHost={match.group(4)}")
                proxy_args.append(f"-Dhttps.proxyPort={match.group(5)}")
        
        # print(f"proxy_args: {proxy_args}")
        return proxy_args
