#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) 2025 THL A29 Limited
#
# This source code file is made available under LGPL License
# See LICENSE for details
# ==============================================================================

import os
import re
import sys
import json
import shlex
import traceback
from shutil import copyfile, rmtree
from time import sleep, time
from multiprocessing import cpu_count
from typing import List

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

import settings
from util.exceptions import CompileTaskError, AnalyzeTaskError, ConfigError, ValidationError, ClientError, ServerError
from util.configlib import ConfigReader
from util.common import (
    SONAR_DEVCOST,
    SONAR_DEBT_RATINGGRID,
    LOCAL_MODEL,
    COMMON_MODEL,
    COMMON_SONAR_LANGS,
    SQBase,
    change_to_win_cmd,
    JVMProxy,
    generate_shell_file,
    Process,
    decode,
)
from util.server import SQServer
from util.api import SQAPIHandler


class Sonar(SQBase):
    def __init__(self):
        super(Sonar, self).__init__()

        self.sleep_second = 5
        self.com_cmd = list()
        self.timeout = 300
        envs = os.environ
        if "SONAR_TIMEOUT" in envs:
            self.timeout = int(envs.get("SONAR_TIMEOUT", self.timeout))
        self.server = SQServer(self.params, self.timeout)

        self.work_dir = os.path.join(self.params["task_dir"], "workdir")
        self.scannerwork = os.path.join(self.work_dir, "scannerwork")

        self.toscan_dir = os.path.join(self.work_dir, "toscan_dir")

    # =================================================================
    # API
    # =================================================================

    def pre_cmd(self, build_cwd):
        """
        执行前置命令
        :return:
        """
        pre_cmd = self.params.get("pre_cmd", None)
        if not pre_cmd:
            return
        print("[warning] do pre_cmd.")
        if isinstance(pre_cmd, str):
            pre_cmd = shlex.split(pre_cmd)
        print("[warning] run pre cmd: %s" % " ".join(pre_cmd))
        Process(
            pre_cmd,
            cwd=build_cwd,
            out=print,
            err=print,
        ).wait()

    def scan_proj(self, scan_fun, languages, **fun_args):
        source_dir = self.source_dir
        work_dir = self.work_dir
        rules = self.params["rules"]
        envs = os.environ
        is_quality = "SONAR_QUALITYPROFILE" in envs or "SONAR_QUALITYPROFILE_TYPE" in envs

        self.server.start(languages)

        if self.server.model == LOCAL_MODEL:
            no_proxy = envs.get("no_proxy", None)
            if no_proxy:
                no_proxy_list = no_proxy.split(",")
            else:
                no_proxy_list = list()
            if "localhost" not in no_proxy_list:
                no_proxy_list.append("localhost")
                envs["no_proxy"] = ",".join(no_proxy_list)
        else:
            sonar_scanner_opts = JVMProxy.get_proxy_args()
            sonar_scanner_opts.append(envs.get("SONAR_SCANNER_OPTS", ""))
            envs["SONAR_SCANNER_OPTS"] = " ".join(sonar_scanner_opts)

        self.com_cmd = self._get_common_cmds()
        self._add_sonar_filter_path()

        self._wait_until_project_create()

        if envs.get("SONAR_DEVCOST", None):
            self.server.sonar_handle.set_settings(
                key="sonar.technicalDebt.developmentCost", value=int(envs.get("SONAR_DEVCOST", SONAR_DEVCOST))
            )
        if envs.get("SONAR_DEBT_RATINGGRID", None):
            self.server.sonar_handle.set_settings(
                key="sonar.technicalDebt.ratingGrid", value=envs.get("SONAR_DEBT_RATINGGRID", SONAR_DEBT_RATINGGRID)
            )

        self._set_qualityprofiles(self.server.sonar_handle, self.server.projectKey, languages)

        sonar_report = scan_fun(**fun_args)
        if envs.get("SONAR_REPORT", None):
            sonar_report = os.path.join(source_dir, envs.get("SONAR_REPORT"))
        if not sonar_report or not os.path.exists(sonar_report):
            print(f"{sonar_report}结果文件不存在，开始遍历查找SQ分析结果文件...")
            sonar_report_list = self.get_dir_files(source_dir, "report-task.txt".lower())
            if self.scannerwork and os.path.exists(self.scannerwork):
                sonar_report_list.extend(self.get_dir_files(self.scannerwork, "report-task.txt".lower()))
            if sonar_report_list:
                sonar_report = sonar_report_list[0]
                print(f"查找到分析文件{sonar_report}")
        print("[info] 结果文件是：%s" % sonar_report)
        self._wait_until_task_succeed(self.server.sonar_handle, sonar_report)

        self._dump_measures(
            self.server.sonar_handle, self.server.projectKey, os.path.join(work_dir, "sonar_result.json")
        )

        issues = self.handle_issues(source_dir, languages, is_quality, rules)

        incr_scan = self.params["incr_scan"]
        if not incr_scan:
            cogn_complex_cnt = 0
            cogn_complex_sum = 0
            cogn_complex_over = 0
            for issue in issues:
                if not issue["rule"].endswith(":S3776"):
                    continue
                # Refactor this method to reduce its Cognitive Complexity from 23 to the 10 allowed.
                msg = issue["msg"]
                info = [token for token in msg.split() if token.isdigit()]
                if len(info) < 2:
                    continue
                cogn_complex_cnt += 1
                cogn_complex_sum += int(info[0])
                cogn_complex_over += int(info[0]) - int(info[1])
            if "summary" not in self.params:
                self.params["summary"] = dict()
            self.params["summary"]["cogncomplexity"] = {
                "over_cognc_func_count": cogn_complex_cnt,
                "over_cognc_func_average": cogn_complex_sum / cogn_complex_cnt if cogn_complex_cnt != 0 else 0,
                "over_cognc_sum": cogn_complex_over,
            }

        if envs.get("SONAR_DEVCOST", None):
            self.server.sonar_handle.set_settings(key="sonar.technicalDebt.developmentCost", value=SONAR_DEVCOST)
        if envs.get("SONAR_DEBT_RATINGGRID", None):
            self.server.sonar_handle.set_settings(key="sonar.technicalDebt.ratingGrid", value=SONAR_DEBT_RATINGGRID)

        if os.path.exists(self.toscan_dir):
            rmtree(self.toscan_dir)

        self.server.sonar_handle.project_delete(project_key=self.server.projectKey)
        print("[warning] Operation after ")

        self.server.close()

        return issues

    # =================================================================
    # common
    # =================================================================

    def handle_issues(self, source_dir: str, languages: str, is_quality: bool, rules: List[str]) -> List:
        pos = len(source_dir) + 1
        envs = os.environ
        build_cwd = envs.get("BUILD_CWD", None)
        build_cwd = os.path.join(source_dir, build_cwd) if build_cwd else source_dir
        issues = []
        try:
            # 指定设置了质量配置文件后，不按照线上规则过滤
            for issue in self.server.sonar_handle.get_issues(
                languages=languages, componentKeys=self.server.projectKey, rules=None if is_quality else ",".join(rules)
            ):
                rule = issue["rule"]
                if not is_quality and rules and rule not in rules:
                    continue
                # gradle编译分析后得到结果可能是相对其中一个模块的相对路径，这里依据build_cwd来做转换
                path = issue["component"].split(":")[-1]
                path = os.path.join(build_cwd, path)[pos:]
                msg = issue["message"]
                text_range = issue.get("textRange", None)
                if text_range:
                    line = int(text_range["startLine"])
                    column = int(text_range["startOffset"])
                else:
                    line = 0
                    column = 0

                # 获取重复代码规则的详细信息
                if rule.endswith("DuplicatedBlocks"):
                    dupl_blocks = self.server.sonar_handle.duplications_show(issue["component"])
                    # 一个个重复链
                    for dupl in dupl_blocks.get("duplications", list()):
                        refs = list()
                        # 重复链中的一个个重复块
                        for block in dupl["blocks"]:
                            refs.append(
                                {
                                    "line": block["from"],
                                    "column": 0,
                                    "msg": "重复块(%d行-%d行)" % (block["from"], block["from"] + block["size"] - 1),
                                    "tag": None,
                                    "path": dupl_blocks["files"][block["_ref"]]["name"],
                                }
                            )
                        # 每个重复链是一个issue
                        issues.append(
                            {"path": path, "rule": rule, "msg": msg, "line": line, "column": column, "refs": refs}
                        )
                else:
                    # 获取问题追溯信息
                    refs = list()
                    for flow in issue.get("flows", []):
                        for location in flow.get("locations", []):
                            refs.append(
                                {
                                    "line": location["textRange"]["startLine"],
                                    "column": location["textRange"]["startOffset"],
                                    "msg": location.get("msg", ""),
                                    "tag": None,
                                    "path": location["component"].split(":")[-1],
                                }
                            )
                    issues.append(
                        {"path": path, "rule": rule, "msg": msg, "line": line, "column": column, "refs": refs}
                    )
        except ValidationError as e:
            # ValidationError: Can return only the first 10000 results. 10100th result asked.
            print("[info] exception: %s" % str(e))

        return issues

    @staticmethod
    def check_usable():
        __class__.init_env()
        check_cmd_args = ["java", "-version"]
        result = True
        try:

            p = Process(check_cmd_args)
            p.wait()
            out = decode(p.p.stdout.read())
            if out.find('version "11.') == -1:
                result = False
        except Exception as err:
            print("tool is not usable: %s" % str(err))
            result = False
        return result

    def get_dir_files(self, root_dir, want_suffix=""):
        files = set()
        for dirpath, _, filenames in os.walk(root_dir):
            for f in filenames:
                if f.lower().endswith(want_suffix):
                    fullpath = os.path.join(dirpath, f)
                    files.add(fullpath)
        files = list(files)
        return files

    def _raise_error(self, msg, proj_del=True, err_type=None):
        """
        抛异常之前先删除对应项目
        :param msg:
        :param proj_del:
        :param err_type:
        :return:
        """
        if proj_del:
            self.server.sonar_handle.project_delete(project_key=self.server.projectKey)
        self.server.close()
        if err_type == "compile":
            raise CompileTaskError(msg)
        elif err_type == "config":
            raise ConfigError(msg)
        else:
            raise AnalyzeTaskError(msg)

    # =================================================================
    # SQ Server
    # =================================================================

    def _wait_until_task_succeed(self, sonar_handle: SQAPIHandler, sonar_report):
        """
        分析之后 到入库会有个时间差。
        等待任务完成。
        :param sonar_handle:
        :param sonar_report:
        :return:
        """
        if not sonar_report or not os.path.exists(sonar_report):
            self._raise_error(f"结果文件({sonar_report})不存在，分析失败，请查看log排查失败原因", err_type="analyze")
        with open(sonar_report) as f:
            id = f.readlines()[4].strip().split("=")[-1]
            print("[warning] Task ID is %s" % id)
        # 检测任务是否执行完成
        timeout = time() + self.timeout
        is_success = False
        while not is_success:
            res = None
            try:
                sleep(self.sleep_second)
                res = sonar_handle.ce_task(id_=id)
                print("[info] Server response is %s" % str(res))
                is_success = True if res["task"]["status"] == "SUCCESS" else False
            except Exception as e:
                # print("[info] exception: %s" % str(e))
                print(f"[error] exception: {traceback.format_exc()}")
                is_success = False

            # 异常捕获，可能sonar服务异常
            if res and res["task"]["status"] == "FAILED":
                if re.match(
                    "load called twice for thread '.*' or state wasn't cleared last time it was used",
                    res["task"]["errorMessage"],
                    re.I,
                ):
                    self._raise_error("SonarQube Server异常，需要重启SonarQube Server。", err_type="analyze")
                elif "Java heap space" == res["task"]["errorMessage"]:
                    # sonarqube server java堆溢出
                    self._raise_error("SonarQube Server异常, Server Java堆溢出异常。", err_type="analyze")
                elif "Unrecoverable indexation failures: 1 errors among 1 requests" == res["task"]["errorMessage"]:
                    self._raise_error(
                        "SonarQube Server异常, 达到文件系统已用空间的85％，90％或95％的elasticsearch操作可能导致索引失败，请检查清理机器存储空间或者重启SonarQube Server。",
                        err_type="analyze",
                    )
                else:
                    self._raise_error("SonarQube Server异常, 请查看log排查。", err_type="analyze")

            # 判断时间戳来判断超时
            if timeout < time():
                self._raise_error("判断任务执行是否执行完成操作超时，请查看log排查原因", err_type="analyze")
        print("[warning] Task completed.")

    def _wait_until_project_create(self):
        """
        创建项目，加上重试逻辑
        因为可能会遇到网络抖动等问题，导致ClientError异常
        :return:
        """
        timeout = time() + self.timeout
        is_project_created = False
        retry_times = 5
        cnt = 0
        print("[warning] Start to create project...")
        while not is_project_created:
            # 创建项目
            # 偶现ClientError异常，尝试重试
            try:
                cnt += 1
                self.server.sonar_handle.project_create(name=self.server.projectKey, project=self.server.projectKey)
                is_project_created = True
            except ValidationError as e:
                # ValidationError: Could not create Project, key already exists: test
                # 这个异常，说明已经创建，可以放过
                print("[info] exception: %s" % str(e))
                is_project_created = True
            except ClientError as e:
                print("[info] exception: %s" % str(e))
                # is_project_created = False
                sleep(self.sleep_second)
            except ServerError as e:
                # 服务器可能会有创建时候数据库异常，进行捕获重试
                # org.h2.jdbc.JdbcSQLIntegrityConstraintViolation Exception: Unique index or primary key violation
                print("[info] exception: %s" % str(e))
                sleep(self.sleep_second)

            # 判断重试次数，超出重试次数则报异常
            if cnt > retry_times:
                self._raise_error(f"SQ项目创建重试超出限制次数{retry_times}次，项目创建失败，请查看log排查原因", err_type="analyze")
            # 判断时间戳来判断超时
            if timeout < time():
                self._raise_error("等待SQ项目创建超时，请查看log排查原因", err_type="analyze")
        print("[warning] Project created success.")

    def _dump_measures(self, sonar_handle, project_key, dump_path):
        """
        获取统计数据
        :param sonar_handle:
        :param project_key:
        :param dump_path:
        :return:
        """
        measures = sonar_handle.get_component_measures(
            metricKeys="ncloc,sqale_index,sqale_debt_ratio,bugs,vulnerabilities,code_smells",
            component=project_key,
            additionalFields="metrics,period",
        )
        print("[info] SQ measures is %s" % str(measures))
        measures_result = dict()
        for index, value in enumerate(measures["component"]["measures"]):
            if value["metric"].startswith("new"):
                measures_result[value["metric"]] = float(value["periods"][0]["value"])
            else:
                measures_result[value["metric"]] = float(value["value"])

            # 获取到的数据便是百分制的
            if value["metric"].endswith("_ratio"):
                measures_result[value["metric"]] = "%.3f%%" % (measures_result[value["metric"]])
            else:
                measures_result[value["metric"]] = int(measures_result[value["metric"]])

        # 传输给summary
        self.params["summary"] = dict()
        self.params["summary"]["sqdebt"] = measures_result

        print("[info] SQ result is %s" % str(measures_result))
        with open(dump_path, "w") as f:
            json.dump(measures_result, f, indent=2)

    def _set_qualityprofiles(self, sonar_handle, project_key, languages):
        """
        设置项目的质量配置
        :param sonar_handle:
        :param project_key:
        :param languages:
        :return:
        """
        source_dir = self.source_dir
        work_dir = self.work_dir
        rules = self.params["rules"]
        rule_list = self.params.get("rule_list", [])
        envs = os.environ
        # sonarqube_home = envs.get("SONARQUBE_HOME")
        root_dir = settings.ROOT_DIR
        langs = languages.split(",")

        # 设置业务自己的质量配置文件时候，替代对应语言配置文件
        default_profiles = self.get_dir_files(
            os.path.join(root_dir, "profiles"), "_SonarQube_Profile.xml".lower()
        )
        qualityprofile_filepaths = dict()
        profiles_path = os.path.join(work_dir, "profiles")
        if not os.path.exists(profiles_path):
            os.mkdir(profiles_path)

        for profile in default_profiles:
            profile_name = os.path.basename(profile)
            lang = profile_name.split("_")[0].lower()
            if self.server.model in (LOCAL_MODEL, COMMON_MODEL) and lang not in COMMON_SONAR_LANGS:
                continue
            profile_path = os.path.join(profiles_path, profile_name)
            copyfile(profile, profile_path)
            qualityprofile_filepaths[lang] = profile_path

        if "SONAR_QUALITYPROFILE_TYPE" in envs:
            print(f"启用{envs.get('SONAR_QUALITYPROFILE_TYPE', '')}模式配置文件")
            for path in self.get_dir_files(
                os.path.join(root_dir, "profiles"), f"_{envs.get('SONAR_QUALITYPROFILE_TYPE', '')}.xml".lower()
            ):
                info = self._get_profile_info(path)
                if info["lang"] not in langs:
                    continue
                profile_name = os.path.basename(path)
                profile_path = os.path.join(profiles_path, profile_name)
                copyfile(path, profile_path)
                qualityprofile_filepaths[info["lang"]] = profile_path

        if envs.get("SONAR_QUALITYPROFILE", None):
            print("[warning] 使用项目指定质量配置文件")
            for path in str(envs.get("SONAR_QUALITYPROFILE")).split(";"):
                profile_path = os.path.join(source_dir, path)
                if not os.path.exists(profile_path):
                    self._raise_error(f"客户自主设置的配置文件({path})不存在, 请客户自查，填写正确的配置文件路径。", err_type="config")
                info = self._get_profile_info(profile_path)
                if info["lang"] not in langs:
                    continue
                qualityprofile_filepaths[info["lang"]] = profile_path

        for lang in qualityprofile_filepaths:
            profile_path = qualityprofile_filepaths[lang]
            if not profile_path.lower().endswith("_SonarQube_Profile.xml".lower()):
                continue
            # 配置规则
            tree = ET.ElementTree(file=profile_path)
            root = tree.getroot()
            all_rules = root.find("rules")
            removed_rules = list()
            for rule in all_rules:
                real_name = "%s:%s" % (rule.find("repositoryKey").text, rule.find("key").text)
                if real_name not in rules:
                    # 梳理没有使用的规则
                    removed_rules.append(rule)
                    continue
                # 在规则列表中的规则，需要设置规则参数
                rule_param = None
                for rule_info in rule_list:
                    if rule_info["name"] == real_name:
                        rule_param = rule_info["params"]
                        break
                if not rule_param:
                    continue
                if "[sq]" not in rule_param:
                    rule_param = "[sq]\n" + rule_param
                rule_params_dict = ConfigReader(cfg_string=rule_param).read("sq")
                if not rule_params_dict:
                    continue
                parameters = rule.find("parameters")
                for parameter in parameters:
                    key = parameter.find("key")
                    value = parameter.find("value")
                    if key.text in rule_params_dict:
                        value.text = rule_params_dict[key.text]

            for rule in removed_rules:
                all_rules.remove(rule)
            tree.write(profile_path)

        # 上传质量配置到Server
        for lang in qualityprofile_filepaths:
            path = qualityprofile_filepaths[lang]
            # print("[warning] 设置项目质量配置文件: %s" % path)
            sonar_handle.qualityprofiles_restore(path)
            # 关联质量配置和项目
            info = self._get_profile_info(path)
            sonar_handle.qualityprofiles_add_project(
                project=project_key, language=info["lang"], qualityProfile=info["name"]
            )

    def _get_profile_info(self, path):
        """
        获取质量配置文件的基本信息
        :param path:
        :return:
        """
        tree = ET.ElementTree(file=path)
        children = tree.getroot().getchildren()
        return {"lang": children[1].text, "name": children[0].text}

    # =================================================================
    # SQ Client
    # =================================================================

    def _get_common_cmds(self):
        """
        获取Sonar客户端的公共options。也可以在项目根目录下的sonar-project.properties文件中配置。
        :return:
        """
        cmds = [
            "-Dsonar.projectKey=%s" % self.server.projectKey,
            "-Dsonar.host.url=%s:%s%s" % (self.server.base_url, str(self.server.port), self.server.base_path),
            "-Dsonar.login=%s" % self.server.user,
            "-Dsonar.password=%s" % self.server.password,
            "-Dsonar.scm.disabled=true",
            "-Dsonar.import_unknown_files=true",
            "-Dsonar.sourceEncoding=UTF-8",
            "-Dsonar.working.directory=%s" % self.scannerwork,
        ]

        # 示例
        # SQ_CLIENT_PARAMS="-Dsonar.javascript.globals=;-Dsonar.javascript.environments="
        if "SQ_CLIENT_PARAMS" in os.environ:
            sonar_params = os.environ.get("SQ_CLIENT_PARAMS", "")
            sonar_params = sonar_params.strip('"').split(";") if sonar_params else []
            cmds.extend(sonar_params)

        return cmds

    def change_to_vs_cmd(self, cmd):
        """
        修改为vs对应的命令格式
        """
        result = list()
        for c in cmd:
            if c.startswith("-Dsonar.projectKey="):
                result.append(f'/k:"{c.split("=")[1]}"')
            elif c.startswith("-D"):
                token = c.split("=")
                result.append(f'/d:{token[0][2:]}="{token[1]}"')
            else:
                result.append(c)
        return result

    def run_cmd(self, command, cwd=None, cmd_type=None):
        """
        运行命令
        :param command:
        :param cwd:
        :param cmd_type:
        :return:
        """
        # print("[warning] run cmd: %s" % " ".join(command))
        print("[warning] Start cmd...")
        spc = Process(
            command,
            cwd,
            out=print,
            err=self.__stderr_handle,
        )
        spc.wait()
        if spc.p.returncode != 0:
            if cmd_type == "compile":
                self._raise_error(msg="编译失败，请确认编译命令正确，并查看log排查失败原因。", err_type=cmd_type)
            # 调整为默认不报异常，只有指定类型才会报异常
            elif cmd_type == "analyze":
                self._raise_error(msg="工具执行分析失败，请查看log排查失败原因。", err_type=cmd_type)

    def __stderr_handle(self, line):
        """
        处理工具执行异常
        :param line:
        :return:
        """
        print(line)
        if line.find("java.lang.IllegalStateException: No files nor directories matching") != -1:
            self._raise_error(msg="Tool_BIN指定的路径下没有找到class文件，请确认Tool_BIN设置正确。", err_type="analyze")
        elif (
            line.find(
                'ERROR: "sonar.cfamily.build-wrapper-output" and "sonar.cfamily.build-wrapper-output.bypass" properties cannot be specified at the same time.'
            )
            != -1
        ):
            self._raise_error(msg="代码库中有Tool参数配置文件，导致执行配置冲突。", err_type="config")
        elif line.find('java.lang.IllegalStateException: The "build-wrapper-dump.json" file was found empty.') != -1:
            self._raise_error(msg="没有监控到编译信息，请依次排查: 1.编译是否成功; 2.编译前是否先执行clean; 3.是否使用的全量编译命令。", err_type="compile")
        elif line.find("java.lang.IllegalStateException: Unable to read file") != -1:
            self._raise_error(msg=f"解析该文件失败，请确保该文件是不是软链接、编码或者语法有问题: {line}", err_type="config")

    def scan_java_proj(self, build_type, build_cwd, build_cmd=None):
        """
        基于构建工具的不同，分析Java项目
        :param build_type:
        :param build_cwd:
        :param build_cmd:
        :return sonar_report: 分析完成的报告，绝对路径
        """
        if build_type.lower() in ("any", "no_build"):
            # 只用到了这个分析器 Sensor JavaSquidSensor [java]
            # 先尝试编译，为了扫描对应的bin文件，获取更准确的结果
            # 没有设置编译命令的话，跳过
            # 编译失败的话，跳过
            if os.environ.get("SQ_JAVA_BUILD") and build_cmd:
                self.run_cmd(command=shlex.split(build_cmd), cwd=build_cwd)
            build_cwd = self.update_sourcedir_while_incr(build_cwd)
            # https://docs.sonarqube.org/display/PLUG/Java+Plugin+and+Bytecode
            scan_cmd = [
                "sonar-scanner",
                "-X",
                "-Dsonar.sources=%s" % os.environ.get("SONAR_JAVA_SRC", "."),
                "-Dsonar.language=java,jsp",
                # sonar.java.binaries，用于方便sq客户端查找java的class和jar文件。侧重于编译模式。
                # 1. 必须配置该参数，否则会出现以下报错
                # Please provide compiled classes of your project with sonar.java.binaries property
                # 2. 必须匹配到实际的路径，否则会出现以下报错
                # java.lang.IllegalStateException: No files nor directories matching 'None'
                # 3. 需要合理配置该option，否则部分规则会失效。
                # 比如 squid:S2159 规则是用于检测两个无关类型的比较场景，如果缺少该option，就会失效。
                # 4. 使用 **/* 的话，会将当前代码库根目录和所有的子目录都配置成 classpath，增加每个文件的分析耗时
                # 跟是否有对应的编译产物无关。
                "-Dsonar.java.binaries=%s" % os.environ.get("SONAR_BIN", "."),
                "-Dsonar.c.file.suffixes=-",
                "-Dsonar.cpp.file.suffixes=-",
                "-Dsonar.objc.file.suffixes=-",
                "-Dsonar.scanner.skipJreProvisioning=true",
                f"-Dsonar.scanner.javaExePath={os.path.join(settings.SQ_JDK_HOME, 'bin', 'java')}",
            ] + self.com_cmd
            if os.environ.get("SONAR_LIB", None):
                scan_cmd.append("-Dsonar.java.libraries=%s" % os.environ.get("SONAR_LIB"))
            # 指定Java版本
            if os.environ.get("SONAR_JAVA_VERSION", None):
                scan_cmd.append("-Dsonar.java.source=%s" % os.environ.get("SONAR_JAVA_VERSION"))
            scan_cmd = change_to_win_cmd(scan_cmd)
            self.run_cmd(command=scan_cmd, cwd=build_cwd, cmd_type="analyze")

            if self.scannerwork and os.path.exists(self.scannerwork):
                return os.path.join(self.scannerwork, "report-task.txt")
            return os.path.join(build_cwd, ".scannerwork", "report-task.txt")

        elif build_type.lower() in ("gradle",):
            if not build_cmd:
                self._raise_error(msg="SQ工具执行Java静态分析时候需要输入编译命令，请填入编译命令后重试。", err_type="compile")
            # 1. 需要现在build.gradle中添加sonar配置
            # plugins {
            #   id "org.sonarqube" version "2.7"
            # }
            # https://docs.sonarqube.org/display/SCAN/Analyzing+with+SonarQube+Scanner+for+Gradle
            # 2. 设置环境变量SONAR_BIN（必填）SONAR_BUILD_TYPE=gralde（必填） SONAR_JAVA_SRC SONAR_LIB
            # 3. 执行gradle命令 gradle sonarqube
            self.run_cmd(
                command=change_to_win_cmd(shlex.split(build_cmd) + ["sonarqube"] + self.com_cmd),
                cwd=build_cwd,
                cmd_type="compile",
            )

            if self.scannerwork and os.path.exists(self.scannerwork):
                return os.path.join(self.scannerwork, "report-task.txt")
            return os.path.join(build_cwd, "build", "sonar", "report-task.txt")

        elif build_type.lower() in ("maven", "mvn"):
            compile_cmd = list()
            if build_cmd:
                # 不支持多行命令，只能是mvn命令
                # self._raise_error(msg="SQ工具执行Java静态分析时候需要输入编译命令，请填入编译命令后重试。", err_type="compile")
                compile_cmd = shlex.split(build_cmd)
            else:
                print("[warning] 没有检测到编译命令，尝试使用默认编译命令。")
                compile_cmd = ["mvn"]
            # 默认是当前目录以缩减耗时，需要的使用可以设置 SONAR_BIN 环境变量进行设置
            compile_cmd.extend(["sonar:sonar", "-Dsonar.java.binaries=%s" % os.environ.get("SONAR_BIN", ".")])
            compile_cmd.extend(self.com_cmd)
            # 需要现在setting.xml和pom.xml中添加sonar配置
            # https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner-for-maven/
            self.run_cmd(command=change_to_win_cmd(compile_cmd), cwd=build_cwd, cmd_type="compile")

        elif build_type.lower() in ("ant",):
            if not build_cmd:
                self._raise_error(msg="SQ工具执行Java静态分析时候需要输入编译命令，请填入编译命令后重试。", err_type="compile")
            # 需要现在build.xml中添加sonar配置,以及sonar参数
            # https://docs.sonarqube.org/display/SCAN/Analyzing+with+SonarQube+Scanner+for+Ant
            self.run_cmd(
                command=change_to_win_cmd(["ant", "sonar", "-v"] + self.com_cmd), cwd=build_cwd, cmd_type="compile"
            )

        else:
            self._raise_error(
                "设置SONAR_BUILD_TYPE异常: 当前SQJava仅支持设置SONAR_BUILD_TYPE为no_build、gradle、maven或ant模式，请检查是否设置错误。",
                err_type="config",
            )

    def scan_cs_vb_proj(self, build_cmd, build_cwd):
        """
        分析C#、Vb项目
        :param build_cmd:
        :param build_cwd:
        :return sonar_report: 分析完成的报告，绝对路径
        """
        if not build_cmd:
            self._raise_error(msg="SQ工具执行C#和Visual Basic静态分析时候需要输入编译命令，请填入编译命令后重试。", err_type="compile")
        # https://docs.sonarqube.org/display/SCAN/Analyzing+with+SonarQube+Scanner+for+MSBuild
        # 1. “classic” .NET Framework
        scan_cmd = [
            "SonarScanner.MSBuild.exe",
            "begin",
            # '/k:"%s"' % self.server.projectKey,
            # '/d:sonar.host.url="%s:%d%s"' % (self.base_url, self.port, self.base_path),
            # '/d:sonar.login="%s"' % self.user,
            # '/d:sonar.password="%s"' % self.password,
            # '/d:sonar.scm.disabled="true"',
        ] + self.change_to_vs_cmd(self.com_cmd)
        self.run_cmd(command=scan_cmd, cwd=build_cwd, cmd_type="compile")
        # build_cmd 这里需要指定sln位置,["MSBuild.exe", path_to_sln, "/t:Rebuild"]
        # 或者是MsBuild编译命令
        self.run_cmd(
            command=shlex.split(generate_shell_file(build_cmd)), cwd=build_cwd, cmd_type="compile"
        )
        self.run_cmd(
            command=[
                "SonarScanner.MSBuild.exe",
                "end",
                '/d:sonar.login="%s"' % self.user,
                '/d:sonar.password="%s"' % self.password,
            ],
            cwd=build_cwd,
            cmd_type="analyze",
        )
        # 分析结果的报告位置
        if self.scannerwork and os.path.exists(self.scannerwork):
            return os.path.join(self.scannerwork, "report-task.txt")
        return os.path.join(build_cwd, ".sonarqube", "out", ".sonar", "report-task.txt")

        # 2. .NET Core

    def scan_cfamily_proj(self, build_type, build_cmd, bw_outputs, build_cwd):
        """
        分析C/C++/OC项目
        :param build_type:
        :param build_cmd:
        :param bw_outputs:
        :param build_cwd:
        :return sonar_report: 分析完成的报告，绝对路径
        """
        # 需要license
        # https://docs.sonarqube.org/display/PLUG/Building+on+Windows

        if sys.platform == "win32":
            build_wrapper = "build-wrapper-win-x86-64.exe"
        elif sys.platform == "darwin":
            build_wrapper = "build-wrapper-macosx-x86"
        else:
            build_wrapper = "build-wrapper-linux-x86-64"

        # 1、无需编译
        # sonar cfamily6.0版本后，不再支持不编译模式，不再支持sonar.cfamily.build-wrapper-output.bypass配置
        if build_type.lower() in ("no_build",):
            scan_cmd = [
                "sonar-scanner",
                "-Dsonar.cfamily.build-wrapper-output.bypass=true",
                "-Dsonar.java.binaries=%s" % os.environ.get("SONAR_BIN", "."),
                "-Dsonar.sources=%s" % os.environ.get("SONAR_CPP_SRC", "."),
                "-Dsonar.scanner.skipJreProvisioning=true",
                f"-Dsonar.scanner.javaExePath={os.path.join(settings.SQ_JDK_HOME, 'bin', 'java')}",
            ] + self.com_cmd
            scan_cmd = change_to_win_cmd(scan_cmd)
            self.run_cmd(command=scan_cmd, cwd=build_cwd, cmd_type="analyze")

            if self.scannerwork and os.path.exists(self.scannerwork):
                return os.path.join(self.scannerwork, "report-task.txt")
            return os.path.join(build_cwd, ".scannerwork", "report-task.txt")

        # 2、非VS编译
        # 在项目根目录创建 sonar-project.properties文件，配置sonar，可以写在命令上
        elif build_type.lower() in ("build",):
            if not build_cmd:
                self._raise_error(msg="SQ工具执行C/C++/OC静态分析时候需要输入编译命令，请填入编译命令后重试。", err_type="compile")
            # 编译捕获
            self.run_cmd(
                command=[build_wrapper, "--out-dir", bw_outputs]
                + shlex.split(generate_shell_file(build_cmd)),
                cwd=build_cwd,
                cmd_type="compile",
            )
            # 执行分析
            scan_cmd = [
                "sonar-scanner",
                "-Dsonar.cfamily.build-wrapper-output=" + bw_outputs,
                "-Dsonar.cfamily.build-wrapper-output.bypass=false",
                "-Dsonar.sources=%s" % os.environ.get("SONAR_CPP_SRC", "."),
                "-Dsonar.cfamily.threads=%s" % str(cpu_count()),
                "-Dsonar.java.binaries=%s" % os.environ.get("SONAR_BIN", "."),
                "-Dsonar.scanner.skipJreProvisioning=true",
                f"-Dsonar.scanner.javaExePath={os.path.join(settings.SQ_JDK_HOME, 'bin', 'java')}",
            ] + self.com_cmd
            self.run_cmd(command=change_to_win_cmd(scan_cmd), cwd=build_cwd, cmd_type="analyze")

            if self.scannerwork and os.path.exists(self.scannerwork):
                return os.path.join(self.scannerwork, "report-task.txt")
            return os.path.join(build_cwd, ".scannerwork", "report-task.txt")

        # 3、VS编译
        # SonarScanner.MSBuild.exe begin /k:"cs-and-cpp-project-key" /n:"My C# and C++ project" /v:"1.0" /d:sonar.cfamily.build-wrapper-output="bw_output"
        elif build_type.lower() in ("vs", "visualstudio", "visual studio"):
            if not build_cmd:
                self._raise_error(msg="SQ工具执行C/C++/OC静态分析时候需要输入编译命令，请填入编译命令后重试。", err_type="compile")
            self.run_cmd(
                command=[
                    "SonarScanner.MSBuild.exe",
                    "begin",
                    # '/k:"%s"' % self.server.projectKey,
                    "/d:sonar.cfamily.build-wrapper-output=%s" % bw_outputs,
                    "/d:sonar.cfamily.build-wrapper-output.bypass=false",
                    # '/d:sonar.host.url="%s:%d%s"' % (self.base_url, self.port, self.base_path),
                    # '/d:sonar.login="%s"' % self.user,
                    # '/d:sonar.password="%s"' % self.password,
                    # '/d:sonar.scm.disabled="true"',
                ]
                + self.change_to_vs_cmd(self.com_cmd),
                cwd=build_cwd,
                cmd_type="compile",
            )
            self.run_cmd(
                command=[build_wrapper, "--out-dir", bw_outputs]
                + shlex.split(generate_shell_file(build_cmd)),
                cwd=build_cwd,
                cmd_type="compile",
            )
            self.run_cmd(command=["SonarScanner.MSBuild.exe", "end"], cwd=build_cwd, cmd_type="analyze")
            # 分析结果的报告位置
            if self.scannerwork and os.path.exists(self.scannerwork):
                return os.path.join(self.scannerwork, "report-task.txt")
            return os.path.join(build_cwd, ".scannerwork", "report-task.txt")

        else:
            self._raise_error(
                "设置SONAR_BUILD_TYPE异常: 当前SQCpp和SQObjectiveC仅支持设置SONAR_BUILD_TYPE为no_build、build或visualstudio模式，请检查是否设置错误。",
                err_type="config",
            )

    def scan_not_build_proj(self, build_cwd):
        """
        分析非编译型语言项目
        :param build_cwd:
        :return sonar_report: 分析完成的报告，绝对路径
        """
        build_cwd = self.update_sourcedir_while_incr(build_cwd)
        scan_cmd = [
            "sonar-scanner",
            "-X",
            "-Dsonar.sources=%s" % os.environ.get("SONAR_SRC", "."),
            # -Dsonar.language 新版已废弃，但遇到java文件时候会自动启动Java分析，要求配置-Dsonar.java.binaries
            "-Dsonar.java.binaries=%s" % os.environ.get("SONAR_BIN", "."),
            "-Dsonar.c.file.suffixes=-",
            "-Dsonar.cpp.file.suffixes=-",
            "-Dsonar.objc.file.suffixes=-",
            "-Dsonar.scanner.skipJreProvisioning=true",
            f"-Dsonar.scanner.javaExePath={os.path.join(settings.SQ_JDK_HOME, 'bin', 'java')}",
        ] + self.com_cmd
        analyze_options = os.environ.get("SQ_ANALYZE_OPTIONS", "")
        if analyze_options:
            scan_cmd.extend(analyze_options.split())
        scan_cmd = change_to_win_cmd(scan_cmd)
        self.run_cmd(command=scan_cmd, cwd=build_cwd, cmd_type="analyze")

        if self.scannerwork and os.path.exists(self.scannerwork):
            return os.path.join(self.scannerwork, "report-task.txt")
        return os.path.join(build_cwd, ".scannerwork", "report-task.txt")

    def update_sourcedir_while_incr(self, build_cwd: str) -> str:
        """
        无需编译模式下，实现增量分析，支持过滤，缩减耗时
        diff文件复制到workdir再扫描。该方法就是用来进行复制的。参考 Cobra/CodeCount/ChangeFunc/ReleaseLint
        - 看是否有结果变化。暂时没有看出变化
        :param params:
        :param scan_cmd:
        :param build_cwd:
        :return:
        """
        source_dir = self.source_dir
        work_dir = self.work_dir
        incr_scan = self.params["incr_scan"]
        # path_filter = FilterPathUtil(self.params)
    
        relpos = len(source_dir) + 1
        if incr_scan:
            # 只有增量情况下
            # diffs = SCMMgr(self.params).get_scm_diff()
            # toscans = [os.path.join(source_dir, diff.path) for diff in diffs if diff.state != "del"]
            toscans = self.diff_files
            # toscans = path_filter.get_include_files(toscans, relpos)
            # 根据 build_cwd 过滤
            toscans = [path for path in toscans if path.startswith(build_cwd)]
            # 调整分隔符
            toscans = [path[relpos:].replace(os.sep, "/") for path in toscans]
            print(f"[info] 待分析文件数是: {len(toscans)}")
    
            if not os.path.exists(self.toscan_dir):
                os.makedirs(self.toscan_dir)
            for path in toscans:
                file_path = os.path.join(self.toscan_dir, path)
                if not os.path.exists(os.path.dirname(file_path)):
                    os.makedirs(os.path.dirname(file_path))
                copyfile(os.path.join(self.source_dir, path), file_path)
            return self.toscan_dir
        else:
            return build_cwd

    def _sonar_path_filter(self, path_list):
        temp = list()
        for path in path_list:
            temp.append(path.replace("*", "***"))
        return temp

    def _sonar_regex_path_filter(self, path_list):
        temp = list()
        for path in path_list:
            temp.append(path.replace(".*", "***"))
        return temp

    def _add_sonar_filter_path(self):
        """
        转换通配符、正则和.code.yml的过滤路径，添加到Sonar的过滤目录中
        :return:
        """
        # 读取三种形式的过滤路径
        path_wild_exclude = self.params["path_filters"].get("wildcard_exclusion", [])
        path_wild_include = self.params["path_filters"].get("wildcard_inclusion", [])
        path_re_exclude = self.params["path_filters"].get("re_exclusion", [])
        path_re_include = self.params["path_filters"].get("re_inclusion", [])
        path_yaml_filters = self.params["path_filters"].get("yaml_filters", {})
        path_yaml_exclude = path_yaml_filters.get("lint_exclusion", [])
        path_yaml_include = path_yaml_filters.get("lint_inclusion", [])

        # 转换路径匹配模式为Sonar的路径匹配模式
        sonar_include = list()
        if path_wild_include:
            sonar_include.extend(self._sonar_path_filter(path_wild_include))
        if path_re_include:
            sonar_include.extend(self._sonar_regex_path_filter(path_re_include))
        if path_yaml_include:
            sonar_include.extend(self._sonar_regex_path_filter(path_yaml_include))
        sonar_exclude = list()
        if path_wild_exclude:
            sonar_exclude.extend(self._sonar_path_filter(path_wild_exclude))
        if path_re_exclude:
            sonar_exclude.extend(self._sonar_regex_path_filter(path_re_exclude))
        if path_yaml_exclude:
            sonar_exclude.extend(self._sonar_regex_path_filter(path_yaml_exclude))

        # 添加到Sonar的过滤目录中
        if sonar_include:
            self.com_cmd.append('-Dsonar.inclusions="%s"' % ",".join(sonar_include))
        if sonar_exclude:
            self.com_cmd.append('-Dsonar.exclusions="%s"' % ",".join(sonar_exclude))


tool = Sonar

if __name__ == "__main__":
    pass
