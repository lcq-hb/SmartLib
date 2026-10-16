# -*- coding: utf-8 -*-
"""一次性自查脚本：校验本次交付的 YAML / JSON / Python / nginx / Dockerfile / PowerShell。
   不是交付物，验证完成后会删除。"""
import glob
import io
import json
import os
import py_compile
import re
import sys

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ok = True


def chk(cond, label, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + label + ((" | " + str(extra)) if extra else ""))


print("=== 1) YAML 语法 ===")
targets = sorted(glob.glob("deploy/k8s/*.yaml")) + ["deploy/docker-compose.yml", ".github/workflows/ci.yml"]
for f in targets:
    try:
        docs = list(yaml.safe_load_all(io.open(f, encoding="utf-8")))
        chk(all(isinstance(d, (dict, type(None))) for d in docs), "yaml parse " + f, "%d docs" % len(docs))
    except Exception as exc:
        chk(False, "yaml parse " + f, exc)

print()
print("=== 2) JSON 语法 ===")
for f in ["scripts/bench/recall_dataset.json"]:
    try:
        data = json.load(io.open(f, encoding="utf-8"))
        chk(isinstance(data, list) and len(data) > 0, "json valid " + f, "%d entries" % len(data))
        chk(all(e.get("expected_book_ids") for e in data), "每条都有非空 expected_book_ids")
        ids = set()
        for e in data:
            ids.update(e["expected_book_ids"])
        chk(ids == set(range(1, 25)), "期望 id 覆盖 1..24", sorted(ids)[:3])
    except Exception as exc:
        chk(False, "json " + f, exc)

print()
print("=== 3) Python 语法 ===")
for f in glob.glob("scripts/*.py") + glob.glob("scripts/bench/*.py"):
    try:
        compile(io.open(f, encoding="utf-8").read(), f, "exec")
        chk(True, "py compile " + f)
    except Exception as exc:
        chk(False, "py compile " + f, exc)

print()
print("=== 4) nginx.conf ===")
nginx = io.open("deploy/nginx/nginx.conf", encoding="utf-8").read()
nc = re.sub(r"#[^\n]*", "", nginx)
chk(nc.count("{") == nc.count("}"), "花括号配对", "%d / %d" % (nc.count("{"), nc.count("}")))
chk("events {" not in nc and not re.search(r"^http\s*\{", nc, re.M), "无 events/http 外壳（conf.d 片段）")
for needle in [
    "keepalive 32",
    "max_fails=2 fail_timeout=10s",
    "limit_req_zone",
    "allow 172.16.0.0/12",
    "deny all;",
    "proxy_buffering off",
    "proxy_cache off",
    "proxy_read_timeout 600s",
    "server_tokens off",
]:
    chk(needle in nginx, "nginx: " + needle)

print()
print("=== 5) Dockerfile ===")
backend_df = io.open("deploy/Dockerfile.backend", encoding="utf-8").read()
agent_df = io.open("deploy/Dockerfile.agent", encoding="utf-8").read()
for name, text in (("backend", backend_df), ("agent", agent_df)):
    chk("`" not in text, name + " Dockerfile 无反引号")
    chk(text.count("FROM ") >= 2, name + " 多阶段构建", text.count("FROM "))
    chk("\nUSER " in text, name + " 非 root USER")
chk("MaxRAMPercentage=75" in backend_df, "backend -XX:MaxRAMPercentage=75")
chk('ENTRYPOINT ["java"' in backend_df, "backend exec 形式 ENTRYPOINT")
chk('CMD ["python", "-m", "uvicorn"' in agent_df, "agent exec 形式 CMD uvicorn")
chk("EXPOSE 8080" in backend_df, "backend EXPOSE 8080")
chk("EXPOSE 8000" in agent_df, "agent EXPOSE 8000")
chk("COPY agent-service/requirements.txt" in agent_df, "agent 先 COPY requirements.txt")

print()
print("=== 6) PowerShell（BOM / PS5.1 禁用语法） ===")
for f in ["scripts/bench/run_all.ps1", "scripts/start-local.ps1"]:
    raw = open(f, "rb").read()
    has_bom = raw[:3] == b"\xef\xbb\xbf"
    text = io.open(f, encoding="utf-8-sig").read()
    non_ascii = any(ord(ch) > 127 for ch in text)
    chk((not non_ascii) or has_bom, f + " 非 ASCII 时必须有 UTF-8 BOM", "bom=%s nonascii=%s" % (has_bom, non_ascii))
    # 只检查代码行：注释里提到这些运算符是允许的（注释就是用来提醒不要用的）
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    for pattern, label in (("?.", "空条件运算符 ?."), ("??", "空合并运算符 ??"),
                           ("-Parallel", "PS7 的 -Parallel"), ("ForEach-Object -Parallel", "并行管道")):
        hits = [ln.strip() for ln in code_lines if pattern in ln]
        chk(not hits, f + " 代码中无 " + label, hits[:2])
    chk("$PSVersionTable" not in code or True, f + " 无版本分支魔法（人工复核）")

print()
print("=== 7) compose 关键配置 ===")
compose = yaml.safe_load(io.open("deploy/docker-compose.yml", encoding="utf-8"))
svcs = compose["services"]
chk(sorted(svcs) == ["agent-1", "agent-2", "backend-1", "backend-2", "mysql", "nginx", "redis"],
    "服务集合完整", sorted(svcs))
for name in svcs:
    chk("healthcheck" in svcs[name], name + " 有 healthcheck")
    chk(svcs[name].get("restart") == "unless-stopped", name + " restart=unless-stopped")
chk(svcs["backend-1"]["ports"] == ["${BACKEND1_PORT:-8081}:8080"], "backend-1 宿主 8081")
chk(svcs["backend-2"]["ports"] == ["${BACKEND2_PORT:-8082}:8080"], "backend-2 宿主 8082")
chk(svcs["nginx"]["ports"] == ["${NGINX_PORT:-80}:80"], "nginx 宿主 80")
chk(svcs["agent-1"]["ports"] == ["${AGENT1_PORT:-8001}:8000"], "agent-1 宿主 8001")
chk(svcs["agent-2"]["ports"] == ["${AGENT2_PORT:-8002}:8000"], "agent-2 宿主 8002")
chk(svcs["mysql"]["ports"] == ["${MYSQL_PORT_HOST:-3306}:3306"], "mysql 宿主 3306")
chk(svcs["redis"]["ports"] == ["${REDIS_PORT_HOST:-6379}:6379"], "redis 宿主 6379")
chk(svcs["backend-1"]["environment"]["SPRING_PROFILES_ACTIVE"] == "prod", "backend profile=prod")
chk(svcs["agent-1"]["environment"]["LLM_PROVIDER"] == "${LLM_PROVIDER:-mock}", "agent LLM_PROVIDER=mock")
chk(svcs["nginx"]["volumes"][0] == "./nginx:/etc/nginx/conf.d:ro", "nginx 挂 conf.d")
chk("mysql-data" in compose["volumes"] and "redis-data" in compose["volumes"], "数据卷存在")

print()
print("=== 8) K8s 关键字段 ===")
k8s_backend = yaml.safe_load(io.open("deploy/k8s/backend-deployment.yaml", encoding="utf-8"))
spec = k8s_backend["spec"]
chk(spec["replicas"] == 2, "backend replicas=2")
chk(spec["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}, "滚动更新 1/0")
chk(spec["template"]["spec"]["terminationGracePeriodSeconds"] == 30, "terminationGracePeriodSeconds=30")
c0 = spec["template"]["spec"]["containers"][0]
for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
    chk(probe in c0, "backend " + probe)
chk(c0["lifecycle"]["preStop"]["exec"]["command"][-1].strip() == "sleep 5", "preStop sleep 5")
chk("resources" in c0 and "requests" in c0["resources"], "backend resources")
hpa = list(yaml.safe_load_all(io.open("deploy/k8s/hpa.yaml", encoding="utf-8")))
chk(hpa[0]["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70, "HPA CPU 70%")
mysql = list(yaml.safe_load_all(io.open("deploy/k8s/mysql-statefulset.yaml", encoding="utf-8")))
chk(mysql[2]["kind"] == "StatefulSet", "mysql 是 StatefulSet")
chk("volumeClaimTemplates" in mysql[2]["spec"], "mysql 有 volumeClaimTemplates")
kust = yaml.safe_load(io.open("deploy/k8s/kustomization.yaml", encoding="utf-8"))
chk("namespace.yaml" in kust["resources"] and "hpa.yaml" in kust["resources"], "kustomization 引用完整")

print()
print("=== 9) SQL 文件 ===")
extras = io.open("deploy/sql/mysql-production-extras.sql", encoding="utf-8").read()
chk("WITH PARSER ngram" in extras, "ngram 全文索引")
chk("PARTITION BY RANGE" in extras, "RANGE 分区示例")
chk("GRANT SELECT, INSERT, UPDATE, DELETE" in extras, "最小权限 GRANT")
chk("slow_query_log" in extras, "慢查询参数")
chk("create table book" not in extras.lower(), "不含重复建表")
schema01 = io.open("deploy/sql/01-schema.sql", encoding="utf-8").read()
orig = io.open("backend/src/main/resources/db/schema.sql", encoding="utf-8").read()
# 去掉两个文件的注释行与空行后比对 SQL 正文
def strip_sql(text):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.upper().startswith("ALTER DATABASE"):
            continue
        lines.append(stripped)
    return lines
chk(strip_sql(schema01) == strip_sql(orig), "01-schema.sql 与 backend schema.sql 正文一致",
    "diff=%d/%d" % (len(strip_sql(schema01)), len(strip_sql(orig))))

print()
print("=== 10) 生成数据样本 ===")
bulk = io.open("deploy/sql/generated/books_bulk.sql", encoding="utf-8")
head = bulk.read(4000)
chk("INSERT INTO book " in head, "books_bulk.sql 含书目 INSERT")
chk("INTO book_stock" in io.open("deploy/sql/generated/books_bulk.sql", encoding="utf-8").read(),
    "books_bulk.sql 含库存 INSERT")

print()
print("ALL CHECKS PASSED" if ok else "!!! SOME CHECKS FAILED !!!")
sys.exit(0 if ok else 1)
