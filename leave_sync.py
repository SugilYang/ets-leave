# -*- coding: utf-8 -*-
"""
다우오피스 휴가신청서 → 실적공수 앱 연차 자동 반영
================================================
매일 06:00(KST) GitHub Actions 가 실행.
  1. GAS 에서 공수 입력 대상자 명단을 받는다 (조직도 − 제외자)
  2. 다우에 로그인해 '휴가 신청서' 통합검색 → 대상자가 기안한 문서만 연다
     - 결재 완료 문서(문서번호 있음)는 seen_docs.json 에 저장해 다음 날 다시 열지 않는다
     - 진행중 문서는 번호가 없으므로 결재가 끝날 때까지 매일 다시 확인 (반려 대비)
  3. 어제 ~ 2주 뒤 범위의 휴가일을 날짜별로 풀어 GAS(leaveSync) 에 전송
     → GAS 가 '연차' 공수 행을 넣고 관리자에게 다우 알림

환경변수(GitHub Secrets): DAOU_URL, DAOU_ID, DAOU_PW, GAS_URL, LEAVE_TOKEN
파싱 로직은 근태 자동화의 daou.py 를 그대로 사용.
"""
import os, sys, json
from datetime import date, datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

from daou import DaouBrowser, parse_search, parse_leave, norm, FORM_LEAVE, REJECT_STATUS

SEEN_FILE = "seen_docs.json"
SEARCH_DAYS = 14          # 최근 N일 기안분만 후보로
WINDOW_BACK, WINDOW_FWD = 1, 14   # 어제 ~ 2주 뒤
KST = timezone(timedelta(hours=9))

SLOT = {"startQuarter1": "오전前", "startQuarter2": "오전後", "startQuarter3": "오후前", "startQuarter4": "오후後",
        "endQuarter1": "오전前", "endQuarter2": "오전後", "endQuarter3": "오후前", "endQuarter4": "오후後"}
HOURS = {"연차": 8, "오전반차": 4, "오후반차": 4, "오전반반차": 2, "오후반반차": 2}


def log(*a):
    print(*a, flush=True)


def env(k):
    v = os.environ.get(k, "").strip()
    if not v:
        log(f"환경변수 {k} 가 없습니다. GitHub Secrets 를 확인하세요.")
        sys.exit(2)
    return v


def gas(url, token, action, **kw):
    r = requests.post(url, json=dict(action=action, token=token, **kw), timeout=180)
    r.raise_for_status()
    return r.json()


def kind_on(rec, d):
    """daou.parse_leave 와 같은 규칙으로 날짜별 휴가 종류 판정 (슬롯 목록만으로)"""
    s, e = date.fromisoformat(rec["start"]), date.fromisoformat(rec["end"])
    slots = rec["start_slots"] if d == s else rec["end_slots"] if d == e else []
    am = [x for x in slots if x.startswith("오전")]
    pm = [x for x in slots if x.startswith("오후")]
    if not slots or len(slots) >= 4:
        return rec["kind"]
    if len(slots) == 2 and len(am) == 2:
        return "오전반차"
    if len(slots) == 2 and len(pm) == 2:
        return "오후반차"
    if len(slots) == 1:
        return "오전반반차" if am else "오후반반차"
    return f"확인필요({'+'.join(slots)})"


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen, today):
    keep = {}
    for k, r in seen.items():
        try:
            if date.fromisoformat(r["end"]) >= today - timedelta(days=30):
                keep[k] = r
        except Exception:
            pass
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=1)


def to_rec(row, html, checked):
    """열어본 휴가신청서 1건 → 저장용 레코드"""
    recs = parse_leave(html, checked, None)
    r = recs[0]
    if not r["start"]:
        return None
    return {
        "name": r["name"], "kind": r["p"] if r["p"] in HOURS else "연차",
        "start": r["start"].isoformat(), "end": (r["end"] or r["start"]).isoformat(),
        "start_slots": [SLOT[c["id"]] for c in checked if c["id"].startswith("startQuarter")],
        "end_slots": [SLOT[c["id"]] for c in checked if c["id"].startswith("endQuarter")],
        "doc_no": row["doc_no"] or f"진행중 {row['status']}", "drafted": row["drafted"].isoformat() if row["drafted"] else "",
    }


def main():
    base_url, uid, pw = env("DAOU_URL"), env("DAOU_ID"), env("DAOU_PW")
    gas_url, token = env("GAS_URL"), env("LEAVE_TOKEN")

    t = gas(gas_url, token, "leaveTargets")
    if not t.get("ok"):
        log("GAS 오류:", t.get("msg")); sys.exit(3)
    targets = {norm(n) for n in t["names"]}
    today = date.fromisoformat(t.get("today") or datetime.now(KST).date().isoformat())
    log(f"기준일 {today} · 공수 입력 대상자 {len(targets)}명")

    seen = load_seen()
    since = today - timedelta(days=SEARCH_DAYS)
    new_recs, opened = [], 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900}, locale="ko-KR")
        br = DaouBrowser(page, base_url, today, log=log)
        if not br.login(uid, pw):
            os.makedirs("debug", exist_ok=True)
            page.screenshot(path="debug/login.png", full_page=True)
            log("다우 로그인 실패 — debug/login.png 확인 (해외 IP 차단·2차 인증 여부)")
            browser.close(); sys.exit(4)

        url = br._search_url("휴가 신청서")
        page.goto(url, wait_until="domcontentloaded"); br._wait(2500)
        rows = parse_search(page.content())
        cands = [r for r in rows
                 if r["form_norm"] == FORM_LEAVE
                 and not any(k in r["status"] for k in REJECT_STATUS)
                 and r["drafted"] and r["drafted"] >= since
                 and norm(r["drafter"]) in targets]
        log(f"검색 {len(rows)}건 → 최근 {SEARCH_DAYS}일 · 대상자 기안 휴가신청서 {len(cands)}건")

        for r in cands:
            if r["doc_no"] and r["doc_no"] in seen:
                continue                                   # 이미 본 완료 문서
            html, checked = br._open_doc(r)
            if html is None:
                log(f"  (클릭 실패: {r['drafter']} {r['drafted']})")
                page.goto(url, wait_until="domcontentloaded"); br._wait(2000)
                continue
            opened += 1
            try:
                rec = to_rec(r, html, checked)
            except Exception as ex:
                log(f"  (파싱 실패: {r['drafter']} {ex})"); rec = None
            if rec:
                log(f"  열람: {rec['name']} {rec['kind']} {rec['start']}~{rec['end']} ({rec['doc_no']})")
                if r["doc_no"]:
                    seen[r["doc_no"]] = rec                # 완료 문서만 기억
                else:
                    new_recs.append(rec)                   # 진행중은 이번 실행에만 사용
            page.goto(url, wait_until="domcontentloaded"); br._wait(2000)
        browser.close()

    log(f"문서 열람 {opened}건 / 기억된 완료 문서 {len(seen)}건")

    # 날짜별로 풀기 (어제 ~ 2주 뒤, 평일만; 공휴일·중복·결재된 주는 GAS 가 거름)
    lo, hi = today - timedelta(days=WINDOW_BACK), today + timedelta(days=WINDOW_FWD)
    items = []
    for rec in list(seen.values()) + new_recs:
        s, e = date.fromisoformat(rec["start"]), date.fromisoformat(rec["end"])
        d = max(s, lo)
        while d <= min(e, hi):
            if d.weekday() < 5:
                k = kind_on(rec, d)
                items.append({"name": rec["name"], "date": d.isoformat(), "kind": k,
                              "hours": HOURS.get(k, 8), "docNo": rec["doc_no"]})
            d += timedelta(days=1)
    log(f"전송 {len(items)}건 ({lo} ~ {hi})")

    res = gas(gas_url, token, "leaveSync", items=items) if items else {"ok": True, "added": [], "skipped": []}
    if not res.get("ok"):
        log("GAS 오류:", res.get("msg")); save_seen(seen, today); sys.exit(5)
    log(f"반영 {len(res['added'])}건: " + (", ".join(res["added"]) or "없음"))
    if res["skipped"]:
        log(f"건너뜀 {len(res['skipped'])}건: " + ", ".join(res["skipped"]))
    save_seen(seen, today)


if __name__ == "__main__":
    main()
