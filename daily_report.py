import json
import os
import requests
import urllib.request
from jinja2 import Template
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from collections import Counter

# ==================== 配置区（从 config.json 读取）====================
def load_config():
    """从 config.json 加载配置，环境变量可覆盖敏感字段"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(config_path):
        print(f"[Error] 配置文件不存在: {config_path}")
        exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 获取当前项目配置（默认第一个项目，可通过 PROJECT_NAME 环境变量切换）
    project_name = os.environ.get("PROJECT_NAME", cfg["projects"][0]["name"])
    project = None
    for p in cfg["projects"]:
        if p["name"] == project_name:
            project = p
            break
    if project is None:
        print(f"[Error] 未找到项目配置: {project_name}")
        exit(1)

    return cfg, project

_cfg, PROJECT_CFG = load_config()

REDMINE_URL = os.environ.get("REDMINE_URL", _cfg["redmine"]["url"] + "/redmine" if not _cfg["redmine"]["url"].endswith("/redmine") else _cfg["redmine"]["url"])
REDMINE_API_KEY = os.environ.get("REDMINE_API_KEY", "")
PROJECT_ID = os.environ.get("REDMINE_PROJECT_ID", PROJECT_CFG["redmine_project_id"])
PROJECT_NAME = PROJECT_CFG["name"]
DISPLAY_NAME = PROJECT_CFG["display_name"]

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", PROJECT_CFG["feishu_webhook"])
REPORT_URL = os.environ.get("REPORT_URL", f"{PROJECT_CFG['report_url_base']}/daily_report_{PROJECT_NAME}.html")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"last_run_state_{PROJECT_NAME}.json")
# ================================================

def fetch_all_redmine_issues():
    """从 Redmine 循环分页拉取所有状态的缺陷（包括 open 和 closed）"""
    if not REDMINE_API_KEY:
        print("[Warn] 未检测到有效的 Redmine API Key。将使用模拟缺陷数据。")
        return [
            {"id": 201, "subject": "【3pro】主控模块偶尔掉线异常", "status": "新建", "priority": "紧急", "assigned_to": "开发 X"},
            {"id": 202, "subject": "【3pro】数据上报协议解析错误", "status": "进行中", "priority": "立刻", "assigned_to": "开发 Y"},
            {"id": 203, "subject": "【3pro】多线程并发读写死锁问题", "status": "已解决", "priority": "高", "assigned_to": "开发 Z"},
        ]

    url = f"{REDMINE_URL}/issues.json"
    headers = {"X-Redmine-API-Key": REDMINE_API_KEY}

    all_issues = []
    limit = 100
    offset = 0

    print("正在从 Redmine 发起缺陷拉取...")
    while True:
        params = {
            "project_id": PROJECT_ID,
            "status_id": "*",
            "sort": "updated_on:desc",
            "limit": limit,
            "offset": offset
        }
        try:
            response = requests.get(url, headers=headers, params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                issues_page = data.get("issues", [])
                total_count = data.get("total_count", 0)

                print(f"成功拉取分页数据：当前已获取 {offset} 到 {offset + len(issues_page)}条 (总计 {total_count} 条)")

                for issue in issues_page:
                    all_issues.append({
                        "id": issue.get("id"),
                        "subject": issue.get("subject"),
                        "status": issue.get("status", {}).get("name", "Unknown"),
                        "priority": issue.get("priority", {}).get("name", "Unknown"),
                        "assigned_to": issue.get("assigned_to", {}).get("name", "未指派"),
                        "updated_on": issue.get("updated_on")
                    })

                if offset + len(issues_page) >= total_count or not issues_page:
                    break

                offset += limit
            else:
                print(f"[Error] 拉取 Redmine 失败，状态码: {response.status_code}")
                break
        except Exception as e:
            print(f"[Error] 访问 Redmine API 异常: {e}")
            break

    return all_issues


def get_last_state_and_update(active_status_counts, active_total, closed_count):
    """
    读取昨日基准并计算变动（每天只更新一次基准，当天多次运行始终与昨日对比）
    状态文件结构：
    {
      "yesterday": {"total": ..., "counts": {...}, "closed_count": ...},
      "yesterday_date": "2026-06-11",
      "today": {"total": ..., "counts": {...}, "closed_count": ...},
      "today_date": "2026-06-12"
    }
    """
    yesterday = {"total": 0, "counts": {}, "closed_count": 0}
    yesterday_date = ""
    today_data = {"total": 0, "counts": {}, "closed_count": 0}
    today_date = ""

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                yesterday = saved.get("yesterday", yesterday)
                yesterday_date = saved.get("yesterday_date", "")
                today_data = saved.get("today", today_data)
                today_date = saved.get("today_date", "")
        except Exception as e:
            print(f"[Warn] 读取状态文件出错: {e}")

    date_str = datetime.now().strftime("%Y-%m-%d")
    current_snapshot = {"total": active_total, "counts": active_status_counts, "closed_count": closed_count}

    if today_date != date_str:
        # 跨天：将旧的 today 滚动为 yesterday，当前数据成为新的 today
        print(f"[State] 检测到新的一天（上次: {today_date or '无'}，今天: {date_str}），更新基准。")
        yesterday = today_data
        yesterday_date = today_date
        today_data = current_snapshot
        today_date = date_str
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "yesterday": yesterday, "yesterday_date": yesterday_date,
                    "today": today_data, "today_date": today_date
                }, f, ensure_ascii=False, indent=2)
            print(f"[State] 基准已更新：昨日 → {yesterday_date}（活跃 {yesterday.get('total', 0)}，Closed {yesterday.get('closed_count', 0)}）")
        except Exception as e:
            print(f"[Error] 保存状态文件失败: {e}")
    else:
        # 同一天内多次运行：只更新 today 数据，yesterday 基准保持不变
        today_data = current_snapshot
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "yesterday": yesterday, "yesterday_date": yesterday_date,
                    "today": today_data, "today_date": today_date
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Error] 保存状态文件失败: {e}")

    # 始终用 yesterday 作为对比基准
    delta = {
        "total": active_total - yesterday.get("total", 0),
        "status_deltas": {},
        "closed_count": closed_count,
        "closed_delta": closed_count - yesterday.get("closed_count", 0)
    }

    all_known_statuses = set(list(active_status_counts.keys()) + list(yesterday.get("counts", {}).keys()))
    for status in all_known_statuses:
        current_val = active_status_counts.get(status, 0)
        last_val = yesterday.get("counts", {}).get(status, 0)
        diff = current_val - last_val
        delta["status_deltas"][status] = {
            "current": current_val,
            "last": last_val,
            "diff": diff,
            "diff_str": f"+{diff}" if diff > 0 else str(diff)
        }

    return delta


def build_summary(issues):
    """统计各维度汇总（响应人、优先级、状态）"""
    # 按响应人统计
    assignee_counter = Counter()
    assignee_details = {}
    for issue in issues:
        person = issue["assigned_to"]
        assignee_counter[person] += 1
        if person not in assignee_details:
            assignee_details[person] = {"total": 0, "priorities": Counter(), "statuses": Counter()}
        assignee_details[person]["total"] += 1
        assignee_details[person]["priorities"][issue["priority"]] += 1
        assignee_details[person]["statuses"][issue["status"]] += 1

    assignee_summary = []
    for person, count in assignee_counter.most_common():
        details = assignee_details[person]
        top_priority = details["priorities"].most_common(1)[0][0] if details["priorities"] else "-"
        top_status = details["statuses"].most_common(1)[0][0] if details["statuses"] else "-"
        assignee_summary.append({
            "name": person,
            "count": count,
            "top_priority": top_priority,
            "top_status": top_status,
            "priority_dist": dict(details["priorities"]),
            "status_dist": dict(details["statuses"])
        })

    # 按优先级统计
    priority_counter = Counter()
    priority_assignees = {}
    for issue in issues:
        pri = issue["priority"]
        priority_counter[pri] += 1
        if pri not in priority_assignees:
            priority_assignees[pri] = Counter()
        priority_assignees[pri][issue["assigned_to"]] += 1

    priority_summary = []
    # 自定义优先级排序（越紧急越靠前）
    priority_rank = {"Urgent": 0, "立刻": 0, "紧急": 1, "High": 1, "高": 2, "中": 3, "Normal": 4, "普通": 4, "Low": 5, "低": 5}
    for pri, _ in sorted(priority_counter.items(), key=lambda x: priority_rank.get(x[0], 99)):
        priority_summary.append({
            "name": pri,
            "count": priority_counter[pri],
            "main_assignees": [{"name": n, "count": c} for n, c in priority_assignees[pri].most_common(3)]
        })

    return assignee_summary, priority_summary


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>每日 Redmine 缺陷追踪简报 - {{ date }}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background-color: #fcfbf9; color: #2c2c2a; margin: 0; padding: 20px; line-height: 1.6; }
        .container { max-width: 800px; margin: 0 auto; background: #ffffff; border: 0.5px solid #d3d1c7; border-radius: 12px; padding: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.02); }
        .header { border-bottom: 2px solid #378add; padding-bottom: 16px; margin-bottom: 24px; }
        .header h1 { font-size: 20px; font-weight: 600; color: #042c53; margin: 0; }
        .header p { font-size: 13px; color: #5f5e5a; margin: 4px 0 0 0; }
        .section-title { font-size: 15px; font-weight: 600; color: #0c447c; border-left: 3px solid #378add; padding-left: 8px; margin: 24px 0 12px 0; }

        /* 统计卡片 */
        .metrics-grid { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
        .metric-card { flex: 1; min-width: 120px; background: #e6f1fb; border-radius: 8px; padding: 12px; border: 0.5px solid #b5d4f4; text-align: center; }
        .metric-card.total-card { background: #eeedfe; border-color: #cecbf6; }
        .metric-card.danger-card { background: #fcebeb; border-color: #f7c1c1; }
        .metric-card.warning-card { background: #faeeda; border-color: #fac775; }
        .metric-card.success-card { background: #eaf3de; border-color: #c0dd97; }
        .metric-label { font-size: 11px; color: #5f5e5a; text-transform: uppercase; margin-bottom: 4px; }
        .metric-value { font-size: 20px; font-weight: 600; color: #2c2c2a; }
        .metric-change { font-size: 11px; margin-top: 4px; font-weight: 500; }
        .text-up { color: #e24b4a; }
        .text-down { color: #1d9e75; }
        .text-flat { color: #888780; }

        /* 列表与表格 */
        table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
        th { background-color: #f1efe8; color: #2c2c2a; font-weight: 500; text-align: left; padding: 10px; border-bottom: 1px solid #d3d1c7; }
        td { padding: 10px; border-bottom: 1px solid #f1efe8; color: #444441; }
        .badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 500; }
        .badge.danger { background-color: #fcebeb; color: #791f1f; }
        .badge.warning { background-color: #faeeda; color: #633806; }
        .badge.success { background-color: #eaf3de; color: #27500a; }
        .badge.info { background-color: #e6f1fb; color: #0c447c; }
        .badge.gray { background-color: #f1efe8; color: #5f5e5a; }

        .footer { font-size: 11px; color: #888780; text-align: center; margin-top: 32px; border-top: 0.5px solid #d3d1c7; padding-top: 16px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🐞 Redmine 缺陷全量追踪与多维分析报告</h1>
            <p>项目标识：<code>{{ project_id }}</code> | 报告生成时间：{{ date }} | 涵盖全部状态（open + closed）| 执勤助手：小八 🤖</p>
        </div>

        <div class="section-title">📊 缺陷分布与昨日变动对比</div>
        <div class="metrics-grid">
            <div class="metric-card total-card">
                <div class="metric-label">活跃缺陷总数（非 Closed）</div>
                <div class="metric-value">{{ active_total }}</div>
                <div class="metric-change">
                    较昨日:
                    {% if delta.total > 0 %}
                        <span class="text-up">+{{ delta.total }} 🔺</span>
                    {% elif delta.total < 0 %}
                        <span class="text-down">{{ delta.total }} 🔻</span>
                    {% else %}
                        <span class="text-flat">无变动 ➖</span>
                    {% endif %}
                </div>
            </div>

            <div class="metric-card success-card">
                <div class="metric-label">Closed（已关闭）</div>
                <div class="metric-value">{{ delta.closed_count }}</div>
                <div class="metric-change">
                    较昨日:
                    {% if delta.closed_delta > 0 %}
                        <span class="text-up">+{{ delta.closed_delta }} 🔺</span>
                    {% elif delta.closed_delta < 0 %}
                        <span class="text-down">{{ delta.closed_delta }} 🔻</span>
                    {% else %}
                        <span class="text-flat">无变动 ➖</span>
                    {% endif %}
                </div>
            </div>

            {% for status, info in delta.status_deltas.items() %}
            <div class="metric-card">
                <div class="metric-label">{{ status }}</div>
                <div class="metric-value">{{ info.current }}</div>
                <div class="metric-change">
                    较昨日:
                    {% if info.diff > 0 %}
                        <span class="text-up">+{{ info.diff }} 🔺</span>
                    {% elif info.diff < 0 %}
                        <span class="text-down">{{ info.diff }} 🔻</span>
                    {% else %}
                        <span class="text-flat">无变动 ➖</span>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="section-title">👤 按响应人汇总 (Assignee Summary)</div>
        <div class="metrics-grid">
            {% for person in assignee_summary %}
            <div class="metric-card">
                <div class="metric-label">{{ person.name }}</div>
                <div class="metric-value">{{ person.count }}</div>
                <div class="metric-change">
                    主要状态: <span class="badge info">{{ person.top_status }}</span>
                    &nbsp;|&nbsp; 主要优先级: <span class="badge warning">{{ person.top_priority }}</span>
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="section-title">🔴 按优先级汇总 (Priority Summary)</div>
        <div class="metrics-grid">
            {% for pri in priority_summary %}
            <div class="metric-card {% if pri.name in ['立刻', '紧急', 'Urgent'] %}danger-card{% elif pri.name in ['高', 'High'] %}warning-card{% else %}success-card{% endif %}">
                <div class="metric-label">{{ pri.name }}</div>
                <div class="metric-value">{{ pri.count }}</div>
                <div class="metric-change">
                    主要指派:
                    {% for a in pri.main_assignees %}
                    {{ a.name }}({{ a.count }})
                    {% endfor %}
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="section-title">📋 活跃缺陷详细清单（共 {{ active_total }} 个，已排除 Closed {{ delta.closed_count }} 个 | 按优先级+ID降序排列）</div>
        <table>
            <thead>
                <tr>
                    <th style="width: 12%">缺陷 ID</th>
                    <th style="width: 42%">主题描述</th>
                    <th style="width: 14%">状态</th>
                    <th style="width: 11%">优先级</th>
                    <th style="width: 11%">指派给</th>
                    <th style="width: 10%">更新时间</th>
                </tr>
            </thead>
            <tbody>
                {% for issue in redmine_issues %}
                <tr>
                    <td><a href="{{ redmine_url }}/issues/{{ issue.id }}" target="_blank" style="color: #378add; text-decoration: none; font-weight: 500;">#{{ issue.id }}</a></td>
                    <td style="font-weight: 450;">{{ issue.subject }}</td>
                    <td><span class="badge info">{{ issue.status }}</span></td>
                    <td>
                        <span class="badge {% if issue.priority in ['立刻', '紧急', '高', 'Urgent', 'High'] %}danger{% else %}gray{% endif %}">
                            {{ issue.priority }}
                        </span>
                    </td>
                    <td>{{ issue.assigned_to }}</td>
                    <td style="font-size: 11px; color: #888780;">{{ issue.updated_on[:10] if issue.updated_on else '-' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="footer">
            此简报由 WorkBuddy 质量智能机器人自动统计生成并发送。<br>
            如需调整报告收件人或每日抓取规则，请在 WorkBuddy 中联系小八修改设置。
        </div>
    </div>
</body>
</html>
"""

def generate_report(send_notifications=True):
    print("开始生成每日测试分析报告...")
    issues = fetch_all_redmine_issues()
    total_count = len(issues)
    print(f"成功收集到全量活跃缺陷共计: {total_count} 个")

    # 统计各状态缺陷数，Closed 单独拿出来不参与变动计算
    status_counts = {}
    closed_count = 0
    for issue in issues:
        status_name = issue["status"]
        if status_name.lower() == "closed":
            closed_count += 1
        else:
            status_counts[status_name] = status_counts.get(status_name, 0) + 1

    active_total = total_count - closed_count

    # 计算与上一次对比的差异 (Delta) - 仅非 Closed 状态参与
    delta_info = get_last_state_and_update(status_counts, active_total, closed_count)

    # 只取非 Closed 的缺陷做响应人/优先级汇总
    active_issues = [i for i in issues if i["status"].lower() != "closed"]
    assignee_summary, priority_summary = build_summary(active_issues)

    # 按优先级排序 (Urgent → High → Normal → Low)，同优先级按 ID 降序（大的在前）
    priority_rank = {"Urgent": 0, "立刻": 0, "紧急": 1, "High": 1, "高": 2, "中": 3, "Normal": 4, "普通": 4, "Low": 5, "低": 5}
    active_issues_sorted = sorted(
        active_issues,
        key=lambda x: (priority_rank.get(x["priority"], 99), -x["id"])
    )

    # 渲染模板
    template = Template(HTML_TEMPLATE)
    html_output = template.render(
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        redmine_issues=active_issues_sorted,
        total_count=total_count,
        active_total=active_total,
        delta=delta_info,
        assignee_summary=assignee_summary,
        priority_summary=priority_summary,
        redmine_url=REDMINE_URL,
        project_id=PROJECT_ID
    )

    # 将输出保存到本地
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_report_3pro.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_output)

    print(f"本地 HTML 报告已成功输出到: {output_path}")

    # 生成报告摘要（保留文本备用）
    text_summary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_summary_3pro.txt")
    with open(text_summary_path, "w", encoding="utf-8") as f:
        f.write(build_feishu_text(issues, total_count, active_total, delta_info, assignee_summary, priority_summary))
    print(f"飞书文本摘要已输出到: {text_summary_path}")

    # 推送飞书消息卡片（含完整报告链接）
    if send_notifications:
        send_feishu_card(total_count, active_total, closed_count, delta_info, assignee_summary, priority_summary, REPORT_URL)

    return output_path


def build_feishu_text(issues, total_count, active_total, delta_info, assignee_summary, priority_summary):
    """构造飞书可用的纯文本消息"""
    lines = []
    lines.append("🐞 Redmine 缺陷追踪日报")
    lines.append(f"项目: {PROJECT_ID} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 总数与变动
    delta_str = f"+{delta_info['total']}" if delta_info['total'] > 0 else str(delta_info['total'])
    closed_delta_str = f"+{delta_info['closed_delta']}" if delta_info['closed_delta'] > 0 else str(delta_info['closed_delta'])
    lines.append(f"📊 缺陷总数: {total_count} (活跃 {active_total}个 + 已关闭 {delta_info['closed_count']}个)")
    lines.append(f"   活跃缺陷较昨日变动: {delta_str}")
    lines.append(f"   Closed较昨日变动: {closed_delta_str}")
    for status, info in delta_info["status_deltas"].items():
        d = info["diff"]
        ds = f"+{d}" if d > 0 else str(d)
        lines.append(f"   - {status}: {info['current']} (较昨日: {ds})")
    lines.append("")

    # 按响应人
    lines.append("👤【按响应人】")
    for p in assignee_summary:
        lines.append(f"   {p['name']}: {p['count']}个 (主要优先级: {p['top_priority']})")
    lines.append("")

    # 按优先级
    lines.append("🔴【按优先级】")
    for p in priority_summary:
        assignees_str = ", ".join([f"{a['name']}({a['count']})" for a in p['main_assignees']])
        lines.append(f"   {p['name']}: {p['count']}个 → {assignees_str}")
    lines.append("")

    lines.append("📋 详情请查看附件 HTML 报告。")

    return "\n".join(lines)


def send_email(report_html_path):
    """SMTP 发送邮件模块"""
    if not SMTP_USER or "user@example.com" in SMTP_USER or not SMTP_PASSWORD or "password_here" in SMTP_PASSWORD:
        print("[Info] 未配置有效的邮件 SMTP 凭证，将略过邮件真实发送。你可以在生成的 HTML 报告中预览效果。")
        return False

    print(f"正在建立与邮件服务器 {SMTP_SERVER}:{SMTP_PORT} 的安全连接...")
    try:
        with open(report_html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"【每日 Redmine 缺陷监控日报 - 3pro】 - {datetime.now().strftime('%Y-%m-%d')}"
        msg["From"] = SMTP_USER
        msg["To"] = ", ".join(REPORT_RECIPIENTS)

        part_html = MIMEText(html_content, "html", "utf-8")
        msg.attach(part_html)

        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()

        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, REPORT_RECIPIENTS, msg.as_string())
        server.quit()
        print("每日缺陷报告邮件已成功发送。")
        return True
    except Exception as e:
        print(f"[Error] 邮件发送失败: {e}")
        return False


def send_feishu_card(total_count, active_total, closed_count, delta_info, assignee_summary, priority_summary, report_url=""):
    """通过飞书自定义机器人 Webhook 发送交互式消息卡片"""
    if not FEISHU_WEBHOOK_URL or "hook/your_webhook" in FEISHU_WEBHOOK_URL:
        print("[Info] 未配置飞书 Webhook URL，跳过飞书推送。")
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 构建状态变化文本
    delta_lines = []
    for status, info in delta_info["status_deltas"].items():
        d = info["diff"]
        arrow = "↑" if d > 0 else ("↓" if d < 0 else "—")
        delta_lines.append(f"**{status}**: {info['current']} ({arrow}{'+' if d > 0 else ''}{d})")

    # 构建响应人文本
    assignee_lines = []
    for p in assignee_summary[:5]:  # 最多显示5人
        assignee_lines.append(f"**{p['name']}**: {p['count']}个")

    # 构建优先级文本
    priority_lines = []
    for p in priority_summary:
        assignees_str = ", ".join([f"{a['name']}({a['count']})" for a in p['main_assignees']])
        priority_lines.append(f"**{p['name']}**: {p['count']}个\n主要指派: {assignees_str}")

    # 活跃总数变化
    total_arrow = "↑" if delta_info["total"] > 0 else ("↓" if delta_info["total"] < 0 else "—")
    total_delta_str = f"{total_arrow}{'+' if delta_info['total'] > 0 else ''}{delta_info['total']}" if delta_info["total"] != 0 else "— 无变动"

    # Closed 数目变化
    closed_arrow = "↑" if delta_info["closed_delta"] > 0 else ("↓" if delta_info["closed_delta"] < 0 else "—")

    # 消息卡片结构
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🐞 Redmine 缺陷追踪日报 | {PROJECT_ID}"},
            "template": "red"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"项目 **{PROJECT_ID}** | {now}"
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**全量缺陷**\n{total_count}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**活跃 (非 Closed)**\n{active_total} ({total_delta_str})"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**Closed (已关闭)**\n{closed_count} ({closed_arrow}{'+' if delta_info['closed_delta'] > 0 else ''}{delta_info['closed_delta']})"
                        }
                    }
                ]
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**按状态分布（较昨日变动）**\n" + "\n".join(delta_lines)
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**按响应人排行**\n" + "\n".join(assignee_lines)
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**按优先级分布**\n" + "\n".join(priority_lines)
                }
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "查看完整 HTML 报告"
                        },
                        "type": "link",
                        "url": report_url,
                        "value": {}
                    }
                ]
            },
            {
                "tag": "note",
                "element": {
                    "tag": "plain_text",
                    "content": "此简报由 WorkBuddy 质量智能机器人自动统计推送 | 每天 9:00 自动更新"
                }
            }
        ]
    }

    payload = {
        "msg_type": "interactive",
        "card": card
    }

    print("[Feishu] 正在推送飞书消息卡片...")
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            FEISHU_WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                print("[Feishu] 飞书消息卡片推送成功！")
                return True
            else:
                print(f"[Warn] 飞书推送返回异常: {result}")
                return False
    except urllib.error.HTTPError as e:
        print(f"[Error] 飞书推送 HTTP 错误: {e.code} {e.reason}")
        return False


if __name__ == "__main__":
    import sys
    no_send = "--no-send" in sys.argv
    report_file = generate_report(send_notifications=not no_send)
    if not no_send:
        send_email(report_file)
