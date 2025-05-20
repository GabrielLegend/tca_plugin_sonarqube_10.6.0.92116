#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================

import os
import sys
import shlex
import random
import psutil
import getpass
from shutil import copyfile, rmtree
from time import sleep, time
from typing import List

import settings
from util.exceptions import CompileTaskError, AnalyzeTaskError, ConfigError
from util.api import SQAPIHandler
from util.common import (
    chmod_ancestor_dir,
    SQ_LOCAL_USER,
    SQ_COMMON_USER,
    COMMON_MODEL,
    LOCAL_MODEL,
    SQBase,
    kill_proc_famliy,
    generate_shell_file,
    Process,
)


class SQRetryError(ConfigError):
    def __init__(self, msg: str):
        ConfigError.__init__(self, f"Got a error when starting sq, retry: {msg}")


class SQServer():
    def __init__(self, params, timeout: int = 300) -> None:
        self.params = params

        # 默认是localhost:9000 admin admin
        # 需要管理员权限，删除项目需要管理员权限
        self.base_url = SQ_LOCAL_USER["url"]
        self.port = SQ_LOCAL_USER["port"]
        self.base_path = SQ_LOCAL_USER["base_path"]
        self.user = SQ_LOCAL_USER["username"]
        self.password = SQ_LOCAL_USER["password"]
        self.projectKey = SQ_LOCAL_USER["projectKey"]
        self.set_api_handler()

        # 默认是local模式，其他的有common
        self.model = LOCAL_MODEL

        self.sleep_second = 5
        self.timeout = timeout
        self.is_local_up: bool = False if settings.PLATFORMS[sys.platform] != "windows" else True
        self.start_exception: Exception = None

        self.java_home = os.environ.get("SQ_JDK_HOME")
        self.sonarqube_home = os.environ.get("SONARQUBE_HOME")

        self.property_path = os.path.join(self.sonarqube_home, "conf", "sonar.properties")
        self.property_temp = os.path.join(self.sonarqube_home, "conf", "sonar.properties.temp")

    def set_api_handler(self):
        if self.password:
            self.sonar_handle = SQAPIHandler(
                host=self.base_url, port=self.port, base_path=self.base_path, user=self.user, password=self.password
            )
        else:
            self.sonar_handle = SQAPIHandler(
                host=self.base_url, port=self.port, base_path=self.base_path, token=self.user
            )

    def set_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def start(self, languages: str, max_times: int = 3) -> None:
        """
        支持多次重试启动
        """
        counter = 1
        while counter <= max_times:
            print(f"[info] The counter of starting sq retry: {counter}")
            try:
                self.try_start(languages)
            except SQRetryError as e:
                # 不捕获超时异常，直接抛出
                self.close()
                if counter == max_times:
                    self._raise_error(str(e), False, "config")
                else:
                    print(f"[warning] {e}")
                    # 出现指定异常之后，需要重试启动
                    counter += 1
                    continue
            # 正常执行就退出
            break

    def try_start(self, languages: str) -> None:
        envs = os.environ
        print("[info] User is %s" % str(getpass.getuser()))

        if "SQ_TYPE" in envs and envs.get("SQ_TYPE") == COMMON_MODEL and SQ_COMMON_USER:
            print("[info] Link common...")
            self._use_common_sonarqube()
        elif sys.platform in ("linux", "linux2") and getpass.getuser() == "root":
            self._root_start_local_sonarqube()
        else:
            self._start_local_sonarqube(
                shlex.split(
                    generate_shell_file(
                        f"export PATH={self.java_home}/bin:$PATH\n./bin/run.sh"
                        if sys.platform != "win32"
                        else f"set PATH={self.java_home}/bin;%PATH%\nbin\\windows-x86-64\\StartSonar.bat"
                    )
                )
            )

        # 验证服务为UP状态
        self._wait_until_sonarqube_on()

    def close(self) -> None:
        """
        关闭服务，恢复现场
        """
        envs = os.environ
        # 关闭SonarQube服务
        if self.model == LOCAL_MODEL:
            self._kill_sonar()

            self.start_exception = None
            if "SONAR_SERVER_PARAMS" in envs and os.path.exists(self.property_temp):
                os.remove(self.property_path)
                os.rename(self.property_temp, self.property_path)

            if os.path.exists(os.path.join(self.sonarqube_home, "data", "sonar.mv.db")):
                os.remove(os.path.join(self.sonarqube_home, "data", "sonar.mv.db"))

    def _use_common_sonarqube(self, model: str = COMMON_MODEL):
        """
        调用公共的sonarqube
        :param model:
        :return:
        """
        sq_user = SQ_COMMON_USER
        self.model = COMMON_MODEL

        self.base_url = sq_user["url"]
        self.port = sq_user["port"]
        self.base_path = sq_user["base_path"]
        self.user = sq_user["username"]
        self.password = sq_user["password"]
        self.projectKey = "%s_%s" % (sq_user["projectKey"], str(self.params.get("project_id", "")))
        self.sonar_handle = SQAPIHandler(host=self.base_url, port=self.port, base_path=self.base_path, token=self.user)
        self.is_local_up = True

    def _root_start_local_sonarqube(self):
        """
        适配root权限下启动sq server
        :return:
        """
        envs = os.environ

        # 指定或者创建非root账户
        sq_user = None
        if "SQ_USER" in envs:
            sq_user = envs.get("SQ_USER")
        else:
            sq_user = "sq"
            Process(
                command=["useradd", sq_user],
                cwd=self.sonarqube_home,
            ).wait()
        Process(
            command=["chmod", "-R", "777", self.sonarqube_home],
            cwd=self.sonarqube_home,
        ).wait()
        Process(
            command=["chmod", "-R", "777", self.java_home],
            cwd=self.sonarqube_home,
        ).wait()
        chmod_ancestor_dir(self.sonarqube_home, 0o777)

        su_cmd = ["sudo", "-u", sq_user, "bash", "-c"]
        has_sudo = Process(
            command=["which", "sudo"],
            cwd=self.sonarqube_home,
        )
        has_sudo.wait()
        if has_sudo.p.returncode != 0:
            su_cmd = ["su", "-c", "-", sq_user]

        return self._start_local_sonarqube(
            su_cmd + ['"PATH=%s/bin:$PATH ./bin/run.sh"' % self.java_home]
        )

    def _start_local_sonarqube(self, cmd):
        """

        :param cmd:
        :return:
        """
        # 启动之前先杀掉本地的sonarqube进程，恢复现场
        self.close()

        envs = os.environ
        # 支持设置sonarqube服务的参数
        if "SONAR_SERVER_PARAMS" in envs:
            # 保存原有配置，便于恢复
            if not os.path.exists(self.property_temp):
                copyfile(self.property_path, self.property_temp)
            # 以分号;分割，比如 SONAR_SERVER_PARAMS=sonar.web.javaOpts=-Xmx512m -Xms128m;sonar.ce.javaOpts=-Xmx512m -Xms128m
            sonar_server_params = envs.get("SONAR_SERVER_PARAMS").strip('"').split(";")
            f = open(self.property_path, "a")
            for param in sonar_server_params:
                f.write("\n%s" % param)
            f.close()

        # print("[info] cmd: %s" % " ".join(cmd))
        spc = Process(
            command=cmd,
            cwd=self.sonarqube_home,
            out=self._start_sonarqube_callback,
            err=self._start_sonarqube_callback,
            shell=True
        )
        timeout = time() + self.timeout
        while not spc.p.pid:
            sleep(self.sleep_second)
            # 判断时间戳来判断超时
            if timeout < time():
                self._raise_error("获取Sq进程PID超时，请查看log排查原因", proj_del=False, err_type="analyze")
        return spc.p.pid

    def _start_sonarqube_callback(self, line):
        """
        监控sq执行情况
        :param line:
        :return:
        """
        print(f"[info] SQServer: {line}")
        address_in_use_error: List[str] = [
            "Caused by: java.net.BindException: Address already in use",
            "Caused by: java.net.BindException: 地址已在使用",
        ]
        error_targets: List[str] = address_in_use_error + [
            "错误: 找不到或无法加载主类 org.sonar.application.App",
            "sudo: pam_open_session: Permission denied",
            "sudo: pam_open_session：拒绝权限",
            "java.lang.IllegalStateException: SonarQube requires Java 11 to run",
            "sudo: sorry, you must have a tty to run sudo",
            "sudo：抱歉，您必须拥有一个终端来执行 sudo",
            "org.elasticsearch.cluster.block.ClusterBlockException: blocked by: [FORBIDDEN/12/index read-only / allow delete (api)];",
            "sudoers.so must be only be writable by owner",
            "fatal error, unable to load plugins",
            "app[][o.s.a.SchedulerImpl] SonarQube is stopped",
        ]
        if self.containAnyString(line, error_targets):
            if SQ_COMMON_USER:
                print("[info] Change to common...")
                self._use_common_sonarqube()
            else:
                if self.start_exception is None:
                    if self.containAnyString(line, address_in_use_error):
                        server_params: List[str] = os.environ.get("SONAR_SERVER_PARAMS", "").split(";")
                        new_server_params: List[str] = list()
                        for param in server_params:
                            if param and param.find(".port=") == -1:
                                new_server_params.append(param)
                        random_numbers = random.sample(range(10000, 65535), 4)
                        self.port = random_numbers[0]
                        print(f"[info] 切换使用端口：{self.port}")
                        new_server_params.extend([
                            f"sonar.web.port={self.port}",
                            f"sonar.embeddedDatabase.port={random_numbers[1]}",
                            f"sonar.search.port={random_numbers[2]}",
                            f"sonar.es.port={random_numbers[3]}",
                        ])
                        os.environ["SONAR_SERVER_PARAMS"] = ";".join(new_server_params)
                        self.set_api_handler()
                    self.start_exception = SQRetryError(line)
        elif line.find("SonarQube is operational") != -1:
            print("[info] Linking Server.")
            self.is_local_up = True

    def containAnyString(self, line: str, targets: List[str]) -> bool:
        for target in targets:
            if line.find(target) != -1:
                return True
        return False

    def _wait_until_sonarqube_on(self):
        """
        等待sonarqube启动完成
        :param sonar_handle:
        :return:
        """
        timeout = time() + self.timeout
        is_server_up = False
        print("[info] Wait for Server...")
        while not is_server_up or not self.is_local_up:
            try:
                sleep(self.sleep_second)
                print(f"[info] Checking {self.model} Status...")
                status = self.sonar_handle.get_system_status().get("status", "DOWN")
                print("[info] Status is %s" % str(status)[0])
                is_server_up = True if status == "UP" else False
            except Exception as e:
                is_server_up = False

            # 判断时间戳来判断超时
            if timeout < time():
                self._raise_error("等待Sq工具启动超时，请查看log排查原因", proj_del=False, err_type="analyze")

            if self.start_exception is not None:
                raise self.start_exception
        print("[info] Server is %s" % str(is_server_up))
        print("[info] Own is %s" % str(self.is_local_up))
        print("[info] Linking Server.")

    def _raise_error(self, msg, proj_del=True, err_type=None):
        """
        抛异常之前先删除对应项目
        :param msg:
        :param proj_del:
        :param err_type:
        :return:
        """
        if proj_del:
            self.sonar_handle.project_delete(project_key=self.projectKey)
        self.close()
        if err_type == "compile":
            raise CompileTaskError(msg)
        elif err_type == "config":
            raise ConfigError(msg)
        else:
            raise AnalyzeTaskError(msg)

    def _kill_sonar(self):
        """
        杀掉sonar的进程
        :return:
        """
        pids = psutil.pids()
        for pid in pids:
            try:
                p = psutil.Process(pid)
                if p.name().lower().startswith("java") and " ".join(p.cmdline()).find("lib/sonar-application") != -1:
                    kill_proc_famliy(pid)
                    break
            except Exception as e:
                print("[info] exception: %s" % str(e))
