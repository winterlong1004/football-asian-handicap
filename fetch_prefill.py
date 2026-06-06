#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_prefill.py — 赛前基本面自动填充工具
===========================================
调用 API-Football (RapidAPI) 自动抓取以下字段并写入 ea_template.json:
  state_factors:
    home_win_streak      主队当前连胜场数
    home_loss_streak     主队当前连败场数
    away_win_streak      客队当前连胜场数
    away_loss_streak     客队当前连败场数
    home_form_score      主队近5场加权评分 (0~1)
    away_form_score      客队近5场加权评分 (0~1)
    home_form_detail     近5场文字描述 (如 "近5场3W1D1L")
    away_form_detail     客队近5场文字描述
    home_ppg             主队主场场均积分
    away_away_ppg        客队客场场均积分

用法:
  python fetch_prefill.py --api-key YOUR_KEY --input data/ea_template.json
  python fetch_prefill.py --api-key YOUR_KEY --input data/ea_template.json --season 2026 --dry-run

API 密钥获取: https://rapidapi.com/api-sports/api/api-football
免费层限制: 100 次/天 (每场比赛约需 4 次请求)

支持联赛 (league_id 映射见 LEAGUE_MAP):
  日职乙 (J2 League): 99
  日职甲 (J1 League): 98
  芬超 (Veikkausliiga): 244
  瑞典超 (Allsvenskan): 113
  中超: 169
  韩K联: 292
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

# ─────────────────────── 联赛 ID 映射 ───────────────────────
LEAGUE_MAP = {
    "日职甲": 98,  "J1": 98,  "J1 League": 98,
    "日职乙": 99,  "J2": 99,  "J2 League": 99,
    "芬超":  244,  "Veikkausliiga": 244,  "Finland": 244,
    "瑞典超": 113, "Allsvenskan": 113,    "Sweden": 113,
    "中超":  169,  "CSL": 169,
    "韩K联": 292,  "K League 1": 292,
    # 五大联赛（备用）
    "英超":  39,   "Premier League": 39,
    "西甲":  140,  "La Liga": 140,
    "德甲":  78,   "Bundesliga": 78,
    "意甲":  135,  "Serie A": 135,
    "法甲":  61,   "Ligue 1": 61,
}

# 近5场加权（最近场次权重最高）
FORM_WEIGHTS = [0.35, 0.25, 0.20, 0.12, 0.08]  # index 0 = 最近一场

BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"


# ─────────────────────── HTTP 请求封装 ───────────────────────
def api_get(endpoint: str, params: dict, api_key: str) -> dict:
    """封装 RapidAPI 请求，返回解析后的 JSON dict"""
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[HTTP Error {e.code}] {endpoint}: {body[:200]}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"[Request Error] {endpoint}: {e}", file=sys.stderr)
        return {}


# ─────────────────────── 球队 ID 查询 ───────────────────────
def find_team_id(team_name: str, league_id: int, season: int, api_key: str) -> Optional[int]:
    """按名称搜索球队，在目标联赛内匹配"""
    data = api_get("teams", {"search": team_name}, api_key)
    teams = data.get("response", [])
    if not teams:
        print(f"  [警告] 未找到球队: {team_name}", file=sys.stderr)
        return None

    # 优先精确匹配
    for t in teams:
        if t["team"]["name"].lower() == team_name.lower():
            return t["team"]["id"]
    # 模糊匹配第一个
    tid = teams[0]["team"]["id"]
    print(f"  [模糊匹配] {team_name} → {teams[0]['team']['name']} (id={tid})", file=sys.stderr)
    return tid


# ─────────────────────── 近期赛果 → 状态连胜/连败/形态 ───────────────────────
def calc_streak_and_form(team_id: int, season: int, league_id: int,
                          venue: Optional[str], api_key: str) -> dict:
    """
    venue: None=全部, "home"=主场, "away"=客场
    返回: {win_streak, loss_streak, form_score, form_detail}
    """
    params = {
        "team": team_id,
        "season": season,
        "league": league_id,
        "last": 20,       # 取最近20场，保证有足够连续记录
        "status": "FT",
    }
    if venue:
        params["venue"] = venue

    data = api_get("fixtures", params, api_key)
    fixtures = data.get("response", [])

    if not fixtures:
        return {
            "win_streak": 0, "loss_streak": 0,
            "form_score": 0.5, "form_detail": "无数据"
        }

    # 按时间倒序（API 通常已倒序，此处保险排序）
    fixtures.sort(key=lambda x: x["fixture"]["timestamp"], reverse=True)

    results = []  # 'W' / 'D' / 'L'
    for fix in fixtures:
        goals = fix.get("goals", {})
        score_home = goals.get("home")
        score_away = goals.get("away")
        if score_home is None or score_away is None:
            continue
        teams = fix.get("teams", {})
        is_home = teams.get("home", {}).get("id") == team_id
        if is_home:
            if score_home > score_away:   results.append("W")
            elif score_home == score_away: results.append("D")
            else:                          results.append("L")
        else:
            if score_away > score_home:   results.append("W")
            elif score_away == score_home: results.append("D")
            else:                          results.append("L")

    if not results:
        return {
            "win_streak": 0, "loss_streak": 0,
            "form_score": 0.5, "form_detail": "无有效赛果"
        }

    # 连胜 / 连败（从最近一场往前数）
    win_streak = 0
    for r in results:
        if r == "W": win_streak += 1
        else: break
    loss_streak = 0
    for r in results:
        if r == "L": loss_streak += 1
        else: break

    # 近5场形态
    recent5 = results[:5]
    w5 = recent5.count("W")
    d5 = recent5.count("D")
    l5 = recent5.count("L")

    # 加权评分 W=1.0 D=0.5 L=0.0
    score_map = {"W": 1.0, "D": 0.5, "L": 0.0}
    weights = FORM_WEIGHTS[:len(recent5)]
    total_w = sum(weights)
    form_score = sum(score_map[r] * w for r, w in zip(recent5, weights)) / total_w if total_w > 0 else 0.5

    venue_label = {"home": "主场", "away": "客场"}.get(venue, "")
    form_detail = f"近{len(recent5)}场{w5}W{d5}D{l5}L" + (f"({venue_label})" if venue_label else "")

    return {
        "win_streak": win_streak,
        "loss_streak": loss_streak,
        "form_score": round(form_score, 2),
        "form_detail": form_detail,
    }


# ─────────────────────── 主客场 PPG 计算 ───────────────────────
def calc_ppg(team_id: int, season: int, league_id: int,
             venue: str, api_key: str) -> Optional[float]:
    """
    venue: "home" | "away"
    返回当前赛季在该场地的场均积分（Win=3, Draw=1, Loss=0）
    """
    params = {
        "team": team_id,
        "season": season,
        "league": league_id,
        "status": "FT",
        "venue": venue,
    }
    data = api_get("fixtures", params, api_key)
    fixtures = data.get("response", [])
    if not fixtures:
        return None

    total_pts = 0
    count = 0
    for fix in fixtures:
        goals = fix.get("goals", {})
        sh, sa = goals.get("home"), goals.get("away")
        if sh is None or sa is None:
            continue
        teams = fix.get("teams", {})
        is_home = teams.get("home", {}).get("id") == team_id
        if (venue == "home" and not is_home) or (venue == "away" and is_home):
            continue
        if is_home:
            if sh > sa:   total_pts += 3
            elif sh == sa: total_pts += 1
        else:
            if sa > sh:   total_pts += 3
            elif sa == sh: total_pts += 1
        count += 1

    if count == 0:
        return None
    return round(total_pts / count, 2)


# ─────────────────────── 主函数 ───────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="赛前基本面自动填充工具 — 调用 API-Football 填充 state_factors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 自动填充 ea_template.json
  python fetch_prefill.py --api-key YOUR_KEY --input data/ea_template.json

  # 预览不写入
  python fetch_prefill.py --api-key YOUR_KEY --input data/ea_template.json --dry-run

  # 指定赛季
  python fetch_prefill.py --api-key YOUR_KEY --input data/ea_template.json --season 2025

  # 覆盖写入另一个文件
  python fetch_prefill.py --api-key YOUR_KEY --input data/ea_template.json --output data/r87.json

API 密钥获取: https://rapidapi.com/api-sports/api/api-football
        """
    )
    parser.add_argument("--api-key", required=True, help="RapidAPI Key (API-Football)")
    parser.add_argument("--input",   required=True, help="输入的 JSON 文件路径 (ea_template.json)")
    parser.add_argument("--output",  default=None,  help="输出路径，默认覆盖原文件")
    parser.add_argument("--season",  type=int, default=None, help="赛季年份 (如 2026)，默认当前年")
    parser.add_argument("--dry-run", action="store_true", help="预览结果，不写入文件")
    args = parser.parse_args()

    # 确定赛季
    import datetime
    season = args.season or datetime.datetime.now().year

    # 读取模板
    if not os.path.exists(args.input):
        print(f"[错误] 文件不存在: {args.input}", file=sys.stderr)
        sys.exit(1)
    with open(args.input, encoding="utf-8") as f:
        template = json.load(f)

    league_name = template.get("league", "")
    home_team   = template.get("home_team", "")
    away_team   = template.get("away_team", "")

    if not league_name or not home_team or not away_team:
        print("[错误] JSON 缺少 league / home_team / away_team 字段", file=sys.stderr)
        sys.exit(1)

    # 联赛 ID 解析
    league_id = LEAGUE_MAP.get(league_name)
    if not league_id:
        print(f"[错误] 未知联赛: {league_name}", file=sys.stderr)
        print(f"  可选联赛: {', '.join(LEAGUE_MAP.keys())}", file=sys.stderr)
        sys.exit(1)

    print(f"📡 联赛: {league_name} (id={league_id})  赛季: {season}")
    print(f"   主队: {home_team}  |  客队: {away_team}")
    print()

    api_key = args.api_key

    # ── 1. 查询球队 ID
    print("🔍 [1/6] 查询主队 ID...")
    home_id = find_team_id(home_team, league_id, season, api_key)
    time.sleep(0.5)

    print("🔍 [2/6] 查询客队 ID...")
    away_id = find_team_id(away_team, league_id, season, api_key)
    time.sleep(0.5)

    if not home_id or not away_id:
        print("[错误] 球队 ID 查询失败，请检查球队名称或手动指定", file=sys.stderr)
        sys.exit(1)

    print(f"   主队 id={home_id}  |  客队 id={away_id}")
    print()

    # ── 2. 主队整体形态（全部比赛，用于连胜/连败）
    print("📊 [3/6] 抓取主队整体近期赛果（连胜/连败/近5场）...")
    home_all = calc_streak_and_form(home_id, season, league_id, None, api_key)
    time.sleep(0.5)

    # ── 3. 客队整体形态
    print("📊 [4/6] 抓取客队整体近期赛果...")
    away_all = calc_streak_and_form(away_id, season, league_id, None, api_key)
    time.sleep(0.5)

    # ── 4. 主队主场 PPG
    print("🏠 [5/6] 计算主队主场 PPG...")
    home_ppg = calc_ppg(home_id, season, league_id, "home", api_key)
    time.sleep(0.5)

    # ── 5. 客队客场 PPG
    print("🏟️  [6/6] 计算客队客场 PPG...")
    away_away_ppg = calc_ppg(away_id, season, league_id, "away", api_key)

    # ── 汇总
    sf_new = {
        "home_win_streak":  home_all["win_streak"],
        "home_loss_streak": home_all["loss_streak"],
        "away_win_streak":  away_all["win_streak"],
        "away_loss_streak": away_all["loss_streak"],
        "home_form_score":  home_all["form_score"],
        "away_form_score":  away_all["form_score"],
        "home_form_detail": home_all["form_detail"],
        "away_form_detail": away_all["form_detail"],
        "home_ppg":         home_ppg,
        "away_away_ppg":    away_away_ppg,
    }

    print()
    print("─" * 50)
    print("✅ 填充结果 (state_factors):")
    for k, v in sf_new.items():
        old = template.get("state_factors", {}).get(k, "—")
        flag = "🔄" if old != v else "  "
        print(f"  {flag} {k:<22} {str(old):>10}  →  {v}")
    print("─" * 50)

    if args.dry_run:
        print("\n[dry-run] 未写入文件")
        return

    # 写入
    template["state_factors"] = sf_new
    output_path = args.output or args.input
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已写入: {output_path}")
    print("   下一步: python euro_asian_task.py --input", output_path)


if __name__ == "__main__":
    main()
