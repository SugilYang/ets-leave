# -*- coding: utf-8 -*-
"""
다우오피스 전자결재 자동 조회 (2단계)
======================================
attendance.py 가 뽑은 '다우 확인 대상'을 자동으로 검색해서
기준일에 해당하는 근태 사유를 찾아냅니다.

대상 문서 3종:
  국내외 출장신청서 / 휴가 신청서 / 사전승인신청서

구조:
  - 파싱 함수(parse_*)는 HTML 문자열만 받는 순수 함수 -> 브라우저 없이 테스트 가능
  - DaouBrowser 는 Playwright 로 로그인/검색/문서열기만 담당

다우 화면 구조 (실제 덤프로 확인된 사실):
  - 검색: /app/approval/search?keyword=... URL 파라미터로 직접 진입 가능
  - 검색결과 td 클래스: date/division/subject/writer/doc_num/state_wrap
  - 제목 <a>는 href 가 비어있어 클릭으로만 진입 가능 (JS 이벤트)
  - 문서 화면에 iframe 없음. 라벨-값이 td 로 나열됨
  - 휴가 반차 체크박스 id: startQuarter1~4 / endQuarter1~4 (값은 한자 오전前/오전後/오후前/오후後)
  - 출장 기간 td 클래스: t_peri1(기존) / t_peri2(변경)
  - 사전승인 td 클래스: attendance_date1/2, attendance_name1(근무자)/2(동승자)
  - 검색은 문서 본문 전체를 뒤지므로, 이름이 참조자로 들어간 남의 문서도 나옴
    -> 반드시 문서를 열어 본문의 실제 대상자를 확인해야 함
  - 기안자 != 출장자인 경우 있음 (출장자가 다수면 최고 선임자가 기안)
  - 진행중 문서는 문서번호가 아직 없음
"""
import re
import json
import os
from datetime import date, timedelta

from bs4 import BeautifulSoup

# 관심 문서 3종 (공백 제거 후 비교. '사전 승인신청서' 변형 존재)
FORM_TRIP, FORM_LEAVE, FORM_PRE = "국내외출장신청서", "휴가신청서", "사전승인신청서"
TARGET_FORMS = {FORM_TRIP, FORM_LEAVE, FORM_PRE}
REJECT_STATUS = ("반려", "반송", "취소", "회수", "삭제")
SEARCH_MONTHS = 3            # 검색 범위(개월)
MAX_DOCS_PER_PERSON = 5      # 1인당 문서 열람 상한 (안전장치)

# 국내 업체명 축약 (P열 코드). 실데이터 변형: 'LG  ES', '오창 엘지에너지 솔루션' 등
COMPANY_TABLE = [
    (["LGES", "LG에너지", "엘지에너지", "LGENERGY", "엘지ES"], "LGES"),
    (["실트론", "SILTRON"], "SK실트론"),
    (["삼성디스플레이", "SDC", "SAMSUNGDISPLAY"], "삼성디스플레이"),
    (["코엠", "COEM", "KOEM"], "코엠"),
    (["LGPRI", "PRI"], "LG PRI"),
]


def norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def parse_date_kr(s):
    """'2026-07-23(목)' -> date"""
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_period(s):
    """'2026-07-23(목) ~ 2026-09-22(화) 61박 62일' -> (date, date). 2자리 연도(26.x.x)도 허용"""
    ds = re.findall(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", str(s or ""))
    if not ds:
        ds = ["20" + m for m in re.findall(r"\b(\d{2}[-./]\d{1,2}[-./]\d{1,2})", str(s or ""))]
    if not ds:
        return None, None
    a = parse_date_kr(ds[0])
    b = parse_date_kr(ds[1]) if len(ds) > 1 else a
    return a, b


def fmt(d):
    return d.strftime("%y.%m.%d") if d else ""


def fmt_period(s, e):
    if s and e and s != e:
        return f"{fmt(s)}~{fmt(e)}"
    return fmt(s)


def short_company(s):
    """업체명 -> P열 코드 축약. 매칭 안 되면 원본 반환"""
    if not s:
        return ""
    t = re.sub(r"[\s\-_/()]+", "", str(s)).upper()
    for keys, val in COMPANY_TABLE:
        if any(k.upper() in t for k in keys):
            return val
    return str(s).strip()


REGION_SHORT = {"충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전라남도": "전남",
                "경상북도": "경북", "경상남도": "경남", "경기도": "경기", "강원도": "강원",
                "강원특별자치도": "강원", "전북특별자치도": "전북", "제주특별자치도": "제주"}


def region_short(addr):
    """'경기도 시흥시 정왕동 ...' -> '경기 시흥' / '서울특별시 강남구' -> '서울 강남'"""
    toks = str(addr or "").split()
    if not toks:
        return ""
    t1 = toks[0]
    p1 = REGION_SHORT.get(t1) or re.sub(r"(특별자치시|특별자치도|특별시|광역시|도|시)$", "", t1)
    p2 = ""
    if len(toks) > 1:
        p2 = re.sub(r"(특별자치시|시|군|구)$", "", toks[1])
    return f"{p1} {p2}".strip()


def clean_names(raw):
    """동승자 문자열 -> 이름 리스트. '無', 'x', '-' 등 제거"""
    out = []
    for c in re.split(r"[,/·、]", str(raw or "")):
        c = c.strip()
        if re.fullmatch(r"[가-힣]{2,5}", c) and c not in ("없음", "없슴"):
            out.append(c)
    return out


# ======================================================================
# 순수 파싱 함수 (HTML 문자열 -> 데이터)
# ======================================================================

def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _clean(s):
    """HTML 특수공백(\xa0) 등을 일반 공백으로 정리"""
    return re.sub(r"\s+", " ", str(s or "").replace("\xa0", " ")).strip()


def label_value(soup, *labels):
    """라벨 td 를 찾아 바로 다음 td 텍스트 반환. 공백 무시 비교"""
    targets = {norm(l) for l in labels}
    for td in soup.find_all(["td", "th"]):
        if norm(td.get_text()) in targets:
            nx = td.find_next_sibling("td")
            if nx:
                return _clean(nx.get_text(" ", strip=True))
    return ""


def class_text(soup, cls):
    el = soup.find("td", class_=cls)
    return _clean(el.get_text(" ", strip=True)) if el else ""


def parse_search(html):
    """검색결과 HTML -> [{drafted, form, form_norm, title, drafter, doc_no, status}]"""
    rows = []
    for tr in _soup(html).find_all("tr"):
        def g(cls):
            el = tr.find("td", class_=cls)
            return el.get_text(" ", strip=True) if el else ""
        title = g("subject")
        if not title:
            continue
        rows.append({
            "drafted": parse_date_kr(g("date")),
            "form": g("division"),
            "form_norm": norm(g("division")),
            "title": title,
            "drafter": g("writer"),
            "doc_no": g("doc_num"),
            "status": g("state_wrap"),
        })
    return rows


def _header(soup):
    return {
        "drafter": label_value(soup, "기안자"),
        "dept": label_value(soup, "부 서", "부서"),
        "doc_no": label_value(soup, "문서번호"),
    }


def parse_trip(html, checked, base):
    """
    국내외 출장신청서 -> 레코드 리스트 (출장자마다 1건)
    checked: 브라우저에서 읽은 체크 상태 [{'id':..,'name':..,'value':..}, ...]
    """
    soup = _soup(html)
    hdr = _header(soup)

    kind = next((c["value"] for c in checked if c["value"] in ("국내", "국외")), "")
    period1 = class_text(soup, "t_peri1")
    period2 = class_text(soup, "t_peri2")
    s, e = parse_period(period1)
    s2, e2 = parse_period(period2)
    if s2:                                   # 변경된 일정이 있으면 우선
        s, e = s2, e2

    company = label_value(soup, "출장 회사명")
    country_raw = label_value(soup, "소재지국")      # '미국 (조지아주)'
    city = label_value(soup, "도착지")               # '서배너'
    project = label_value(soup, "프로젝트명")        # 'ESHG 1,2호기'
    country = re.split(r"[\s(]", country_raw.strip())[0] if country_raw else ""

    # 출장자 행 파싱.
    # 구조: rownum1(기 존) 헤더 -> 기존 출장자 행들 / rownum2(변 경) 헤더 -> 변경 행들
    # !! 일정 변경 시 '변경' 행에 새 일정이 적힘 -> 이름이 같으면 변경 일정이 기존을 덮어씀 !!
    def walk_rows(anchor_cls):
        out = []
        anchor = soup.find("td", class_=anchor_cls)
        if not anchor:
            return out
        tr = anchor.find_parent("tr")
        while tr is not None:
            tr = tr.find_next_sibling("tr")
            if tr is None:
                break
            tds = tr.find_all("td")
            if len(tds) < 2:
                break
            first = tds[0].get_text(" ", strip=True)
            if re.search(r"출장\s*회사명|소재지국|출장자|출발|경유|도착지|출장형태|추가|삭제", first):
                break
            nm = _clean(tds[1].get_text(" ", strip=True))
            per_el = tr.find("td", class_=re.compile(r"t_peri"))
            per = per_el.get_text(" ", strip=True) if per_el else ""
            if nm:
                out.append({"name": nm, "dept": first, "period": per})
        return out

    travelers = walk_rows("rownum1")                 # 기존
    for ch in walk_rows("rownum2"):                  # 변경 -> 덮어쓰기
        cs, _ = parse_period(ch["period"])
        if cs is None:
            continue                                 # 변경 일정이 비어있으면 무시
        hit = next((t for t in travelers if norm(t["name"]) == norm(ch["name"])), None)
        if hit:
            hit["period"] = ch["period"]
        else:
            travelers.append(ch)
    if not travelers:
        travelers = [{"name": hdr["drafter"], "dept": hdr["dept"], "period": period1}]

    if kind == "국외":
        p_val = f"해외-{country}" if country else "해외출장"
        q_head = " ".join(x for x in (city, project) if x).strip()
    else:
        biz = ""
        for cand in (company, project, city):
            got = short_company(cand)
            if any(got == v for _, v in COMPANY_TABLE):
                biz = got
                break
        p_val = f"국내-{biz}" if biz else f"국내-{(company or project).strip()}" if (company or project) else "국내출장"
        q_head = ""

    recs = []
    for t in travelers:
        ts, te = parse_period(t["period"])
        if ts is None:
            ts, te = s, e
        q = f"{q_head} {fmt_period(ts, te)}".strip() if q_head else fmt_period(ts, te)
        rec = {"name": t["name"], "form": FORM_TRIP, "p": p_val, "q": q,
               "start": ts, "end": te, "doc_no": hdr["doc_no"]}
        # 해외출장: 종료일에 출국해도 한국 도착은 다음 날일 수 있음.
        # 날짜는 문서 그대로 적고, 비고에 확인 요망만 남긴다 (임의 변경 금지)
        if kind == "국외":
            rec["note"] = "귀국일 확인 요망 (도착이 다음날일 수 있음)"
        recs.append(rec)
    return recs


def parse_leave(html, checked, base):
    """휴가 신청서 -> 레코드 1건 (기안자 본인)"""
    soup = _soup(html)
    hdr = _header(soup)
    kind = label_value(soup, "휴가 종류") or "연차"
    period = label_value(soup, "기간 및 일시")
    s, e = parse_period(period)

    # 반차 슬롯: 한자 주의 (오전前/오전後/오후前/오후後)
    slot_label = {"startQuarter1": "오전前", "startQuarter2": "오전後",
                  "startQuarter3": "오후前", "startQuarter4": "오후後",
                  "endQuarter1": "오전前", "endQuarter2": "오전後",
                  "endQuarter3": "오후前", "endQuarter4": "오후後"}
    start_slots = [slot_label[c["id"]] for c in checked
                   if c["id"].startswith("startQuarter")]
    end_slots = [slot_label[c["id"]] for c in checked
                 if c["id"].startswith("endQuarter")]

    # 기준일이 시작일이면 시작 슬롯, 종료일이면 종료 슬롯, 중간일이면 종일
    slots = start_slots if (s and base == s) else end_slots if (e and base == e) else []

    am = [x for x in slots if x.startswith("오전")]
    pm = [x for x in slots if x.startswith("오후")]
    if not slots or len(slots) >= 4:
        p_val = kind
    elif len(slots) == 2 and len(am) == 2:
        p_val = "오전반차"
    elif len(slots) == 2 and len(pm) == 2:
        p_val = "오후반차"
    elif len(slots) == 1:
        p_val = "오전반반차" if am else "오후반반차"
    else:
        p_val = f"확인필요({'+'.join(slots)})"

    return [{"name": hdr["drafter"], "form": FORM_LEAVE, "p": p_val,
             "q": fmt_period(s, e), "start": s, "end": e, "doc_no": hdr["doc_no"]}]


def parse_pre(html, checked, base):
    """사전승인신청서 -> 레코드 리스트 (근무자 + 동승자). 외근은 제외"""
    soup = _soup(html)
    hdr = _header(soup)

    kinds = ("직출/직퇴", "직출", "직퇴", "외근", "기타")
    kind = next((c["value"] for c in checked if c["value"] in kinds), "")
    reason = label_value(soup, "사 유", "사유")
    is_edu = "교육" in reason                # 사유란에 '...교육' 문구 -> 교육 참석
    if kind in ("외근", "") and not is_edu:
        return []                            # 외근 = 정상 출퇴근, 근태 대상 아님

    d1 = class_text(soup, "attendance_date1")
    d2 = class_text(soup, "attendance_date2")
    s = parse_date_kr(d1)
    e = parse_date_kr(d2) or s
    worker = class_text(soup, "attendance_name1") or hdr["drafter"]
    companions = clean_names(class_text(soup, "attendance_name2"))
    company = label_value(soup, "업체명")
    project = label_value(soup, "ProjectName", "Project Name")

    biz = ""
    for cand in (company, project):
        got = short_company(cand)
        if any(got == v for _, v in COMPANY_TABLE):
            biz = got
            break
    p_val = f"국내-{biz}" if biz else (f"국내-{company.strip()}" if company else "국내출장")
    q = fmt_period(s, e)
    region = region_short(label_value(soup, "업체주소", "업체 주소"))
    if is_edu:
        # 교육: 보고서 표기 형식 그대로 Q에 담음 -> 기간/지역/교육명
        p_val = "교육"
        parts = [fmt_period(s, e)]
        if region:
            parts.append(region.replace(" ", ""))
        if reason.strip():
            parts.append(reason.strip())
        q = "/".join(parts)

    recs = [{"name": worker, "form": FORM_PRE, "p": p_val, "q": q, "region": region,
             "start": s, "end": e, "doc_no": hdr["doc_no"]}]
    for c in companions:
        recs.append({"name": c, "form": FORM_PRE, "p": p_val, "q": q, "region": region,
                     "start": s, "end": e, "doc_no": hdr["doc_no"]})
    return recs


def covers(rec, d):
    return rec["start"] is not None and rec["start"] <= d <= (rec["end"] or rec["start"])


FORM_PRIORITY = {FORM_PRE: 3, FORM_LEAVE: 2, FORM_TRIP: 1}


def pick_best(records):
    """같은 사람 여러 문서 -> 기간 긴 것 > 사전승인 > 휴가 > 출장"""
    if not records:
        return None
    def days(r):
        return ((r["end"] or r["start"]) - r["start"]).days if r["start"] else 0
    ranked = sorted(records, key=lambda r: (days(r), FORM_PRIORITY.get(r["form"], 0)),
                    reverse=True)
    best = dict(ranked[0])
    if len(ranked) > 1:
        best["conflict"] = " / ".join(
            f"{o['form']}({o['p']} {fmt_period(o['start'], o['end'])})" for o in ranked[1:])
    return best


# ======================================================================
# 브라우저 (Playwright)
# ======================================================================

class DaouBrowser:
    def __init__(self, page, base_url, base_date, log=print):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.base = base_date
        self.log = log
        self._cache = {}         # doc_no/제목 -> 레코드 리스트

    def _wait(self, ms=2000):
        self.page.wait_for_timeout(ms)     # 다우는 networkidle 금지, 고정 대기

    def login(self, user_id, password):
        self.log(f"  [다우] 로그인 시도: {user_id}")
        self.page.goto(f"{self.base_url}/login", wait_until="domcontentloaded")
        self._wait(2500)
        try:
            self.page.fill("input[type='password']", password)
            ids = self.page.locator("input[type='text']")
            if ids.count() > 0:
                ids.first.fill(user_id)
            self._wait(300)
            self.page.locator("input[type='password']").press("Enter")
            self._wait(5000)
        except Exception as ex:
            self.log(f"  [다우] 자동 로그인 실패: {ex}")
            return False
        ok = "/login" not in self.page.url
        self.log(f"  [다우] 로그인 {'성공' if ok else '실패'}")
        return ok

    def _search_url(self, name):
        from urllib.parse import quote
        frm = self.base - timedelta(days=30 * SEARCH_MONTHS)
        return (f"{self.base_url}/app/approval/search?stype=simple&type=approval"
                f"&keyword={quote(name)}"
                f"&fromDate={frm.isoformat()}T00%3A00%3A00.000%2B09%3A00"
                f"&toDate={self.base.isoformat()}T23%3A59%3A59.000%2B09%3A00"
                f"&page=0&offset=100")

    def _read_checked(self):
        """현재 문서 화면의 체크된 checkbox/radio 상태"""
        return self.page.evaluate("""
        () => Array.from(document.querySelectorAll('input:checked'))
                   .map(el => ({id: el.id || '', name: el.name || '', value: el.value || ''}))
        """)

    def _open_doc(self, target):
        """검색결과에서 문서번호(또는 제목+기안일)로 행을 찾아 제목 클릭.
        진행중 문서는 문서번호가 없어 제목으로 찾는다. 이때
        - 다우가 검색어 강조로 이름 앞뒤에 공백을 끼워넣으므로 공백 제거 비교
        - 목록이 긴 제목을 '…'로 잘라 보여주므로 완전일치가 아니라 접두 비교"""
        ok = self.page.evaluate("""
        (t) => {
          const squash = s => (s || '').replace(/\\s+/g, '');
          for (const tr of document.querySelectorAll('table tr')) {
            const no = tr.querySelector('td.doc_num');
            const subj = tr.querySelector('td.subject');
            if (!subj) continue;
            const noTxt = no ? no.innerText.trim() : '';
            let hit;
            if (t.doc_no) {
              hit = (noTxt === t.doc_no);
            } else {
              const raw = squash(subj.innerText);
              const wasTrunc = /(…|\\.\\.\\.)$/.test(raw);
              const rs = raw.replace(/[…]+$/, '').replace(/\\.\\.\\.$/, '');
              // 완전일치 (짧은 제목 '휴가 신청서' 등) 또는
              // 잘린 제목('...끝이 …')일 때만 접두 일치
              hit = (rs === t.title_squash) ||
                    (wasTrunc && rs.length >= 8 && t.title_squash.startsWith(rs));
              if (hit && t.drafter) {
                // 같은 제목의 진행중 문서가 여럿일 수 있으니 기안자까지 대조
                const w = tr.querySelector('td.writer');
                if (w && squash(w.innerText) !== t.drafter) hit = false;
              }
            }
            if (hit) {
              const a = subj.querySelector('a') || subj;
              a.click();
              return true;
            }
          }
          return false;
        }
        """, {"doc_no": target["doc_no"], "title_squash": norm(target["title"]),
              "drafter": norm(target.get("drafter", ""))})
        if not ok:
            return None, None
        self._wait(3000)
        html = self.page.content()
        checked = self._read_checked()
        return html, checked

    # ---- 휴가신청서 통합 검색 (ERP 대체) ----
    _CACHE_FILE = "다우캐시.json"

    def _cache_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), self._CACHE_FILE)

    def _load_disk_cache(self):
        try:
            with open(self._cache_path(), encoding="utf-8") as f:
                raw = json.load(f)
            out = {}
            for k, recs in raw.items():
                out[k] = [dict(r, start=date.fromisoformat(r["start"]) if r.get("start") else None,
                               end=date.fromisoformat(r["end"]) if r.get("end") else None)
                          for r in recs]
            return out
        except Exception:
            return {}

    def _save_disk_cache(self, cache):
        try:
            raw = {k: [dict(r, start=r["start"].isoformat() if r.get("start") else None,
                            end=r["end"].isoformat() if r.get("end") else None)
                       for r in recs]
                   for k, recs in cache.items()}
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
        except Exception:
            pass

    def scan_leaves(self, days=14):
        """'휴가 신청서' 통합 검색 -> 최근 days일 기안분을 전부 열어
        기준일을 커버하는 휴가를 {이름: rec} 로 반환 (완료·진행중 모두).
        완료 문서(문서번호 있음)는 디스크 캐시에 저장해 다음 날 다시 열지 않는다."""
        since = self.base - timedelta(days=days)
        self.log(f"  [다우] 휴가신청서 통합 검색 (기안일 {since} 이후 전부 확인)")
        url = self._search_url("휴가 신청서")
        self.page.goto(url, wait_until="domcontentloaded")
        self._wait(2500)
        rows = parse_search(self.page.content())
        cands = [r for r in rows
                 if r["form_norm"] == FORM_LEAVE
                 and not any(k in r["status"] for k in REJECT_STATUS)
                 and r["drafted"] and r["drafted"] >= since]
        cands.sort(key=lambda r: r["drafted"], reverse=True)
        if rows and len(rows) >= 100 and rows[-1]["drafted"] and rows[-1]["drafted"] >= since:
            self.log(f"      ⚠ 검색 결과 100건이 전부 {days}일 이내 -> 일부 누락 가능 (days 축소 검토)")
        self.log(f"      검색 {len(rows)}건 -> 최근 {days}일 휴가신청서 {len(cands)}건")

        disk = self._load_disk_cache()
        out, opened, from_cache = {}, 0, 0
        for r in cands:
            ckey = r["doc_no"] or f"{r['drafted']}|{r['title']}|{r['drafter']}"
            if r["doc_no"] and ckey in disk:
                recs = disk[ckey]
                from_cache += 1
            elif ckey in self._cache:
                recs = self._cache[ckey]
            else:
                html, checked = self._open_doc(r)
                if html is None:
                    self.log(f"      (클릭 실패: {r['drafter']} {r['drafted']} {r['title'][:20]})")
                    self.page.goto(url, wait_until="domcontentloaded")
                    self._wait(2000)
                    continue
                opened += 1
                try:
                    recs = parse_leave(html, checked, self.base)
                except Exception as ex:
                    self.log(f"      (파싱 실패: {ex})")
                    recs = []
                for rec in recs:
                    if not rec["doc_no"]:
                        rec["doc_no"] = f"(진행중 {r['status']})"
                self._cache[ckey] = recs
                if r["doc_no"]:
                    disk[ckey] = recs
                self.page.goto(url, wait_until="domcontentloaded")
                self._wait(2000)
            for rec in recs:
                if covers(rec, self.base) and rec["name"] not in out:
                    out[rec["name"]] = rec       # 최신 기안이 우선
        self._save_disk_cache(disk)
        self.log(f"      문서 열람 {opened}건 / 캐시 {from_cache}건 -> 오늘 휴가자 {len(out)}명")
        for nm, rec in sorted(out.items()):
            self.log(f"        · {nm}  {rec['p']} | {rec['q']}  ({rec['doc_no']})")
        return out

    def lookup(self, names):
        """
        이름 리스트 -> {이름: 레코드 or None}
        레코드: {p, q, form, doc_no, start, end, conflict?}
        """
        result = {}
        cutoff = self.base - timedelta(days=30 * SEARCH_MONTHS)

        for name in names:
            self.log(f"\n  [다우] '{name}' 검색")
            self.page.goto(self._search_url(name), wait_until="domcontentloaded")
            self._wait(2500)
            rows = parse_search(self.page.content())

            # ---- 문서를 '열지 않고' 거르는 단계 ----
            # 다우 검색은 본문 전체를 뒤지므로 참조자로 이름만 걸린 남의 문서가 섞여 나옴.
            # 구버전은 이런 걸 전부 열어봐서 느렸음. 유형별 성질로 안 열고 거른다:
            #   3종 외 양식        -> 제외
            #   반려/취소/오래됨   -> 제외
            #   휴가신청서         -> 본인 기안 문서만 (기안자 != 이름 -> 제외)
            #   사전승인신청서     -> 제목에 이름·기간이 있음 -> 이름 없거나 기간 밖 -> 제외
            #   출장신청서         -> 제목에 정보 없음 -> 열어야 함 (단 조기종료로 최소화)
            cands = []
            n_form = n_stat = n_old = n_writer = n_title = 0
            for r in rows:
                if r["form_norm"] not in TARGET_FORMS:
                    n_form += 1
                    continue
                if any(k in r["status"] for k in REJECT_STATUS):
                    n_stat += 1
                    continue
                title_covers = False
                if r["form_norm"] == FORM_PRE:
                    # 제목의 기간만 검사. 이름 검사는 하지 않는다:
                    # 다우 목록이 긴 제목을 잘라서 이름이 안 보일 수 있어 오탐 위험
                    ts, te = parse_period(r["title"])
                    if ts is not None:
                        if not (ts <= self.base <= (te or ts)):
                            n_title += 1       # 기간이 기준일 밖
                            continue
                        title_covers = True    # 제목 기간이 오늘을 커버
                # 오래된 문서 거르기 - 단, 장기 사전승인(제목 기간이 오늘 커버)은
                # 몇 달 전 기안이라도 열어야 함
                if r["drafted"] and r["drafted"] < cutoff and not title_covers:
                    n_old += 1
                    continue
                if r["form_norm"] == FORM_LEAVE and norm(r["drafter"]) != norm(name):
                    n_writer += 1          # 남의 휴가신청서 (참조로 걸림)
                    continue
                cands.append(r)

            skipped = len(rows) - len(cands)
            self.log(f"      검색 {len(rows)}건 -> 열람 후보 {len(cands)}건 "
                     f"(안 열고 거름 {skipped}: 타양식{n_form}/반려{n_stat}/오래됨{n_old}"
                     f"/남의휴가{n_writer}/제목불일치{n_title})")

            # 최신 기안 순으로 열람. 일정 변경 시 재기안하므로 최신 문서가 정답.
            cands.sort(key=lambda r: r["drafted"] or date.min, reverse=True)

            found = []
            opened = 0
            for r in cands:
                # 기준일 커버 문서를 찾는 즉시 종료 -> 다른 결재를 들춰보지 않음
                if found:
                    self.log(f"      (사유 확보 -> 남은 문서 {len(cands)-opened}건 열람 안 함)")
                    break
                if opened >= MAX_DOCS_PER_PERSON:
                    self.log(f"      (안전장치: {opened}건 열람 후 중단)")
                    break
                ckey = r["doc_no"] or f"{r['drafted']}|{r['title']}"
                if ckey in self._cache:
                    recs = self._cache[ckey]
                else:
                    self.log(f"      열람: [{r['form']}] {r['title'][:40]}")
                    html, checked = self._open_doc(r)
                    opened += 1
                    if html is None:
                        self.log("      (클릭 실패, 다음 문서로)")
                        continue
                    try:
                        if r["form_norm"] == FORM_TRIP:
                            recs = parse_trip(html, checked, self.base)
                        elif r["form_norm"] == FORM_LEAVE:
                            recs = parse_leave(html, checked, self.base)
                        else:
                            recs = parse_pre(html, checked, self.base)
                    except Exception as ex:
                        self.log(f"      (파싱 실패: {ex})")
                        recs = []
                    for rec in recs:
                        if not rec["doc_no"]:
                            rec["doc_no"] = f"(진행중 {r['status']})"
                    self._cache[ckey] = recs
                    # 검색 화면 복귀
                    self.page.goto(self._search_url(name), wait_until="domcontentloaded")
                    self._wait(2000)

                # 본문의 실제 대상자 + 기준일 커버 여부로 판정
                for rec in recs:
                    if norm(rec["name"]) == norm(name) and covers(rec, self.base):
                        found.append(rec)

            best = pick_best(found)
            if best:
                self.log(f"      ✓ {best['p']} | {best['q']}  ({best['form']})")
            else:
                self.log(f"      ✗ 기준일 해당 문서 없음")
            result[name] = best
        return result
