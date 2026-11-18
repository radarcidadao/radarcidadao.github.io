#!/usr/bin/env python3
"""
Motor coletor para a Câmara Municipal de Caraguatatuba (15 vereadores).
Consome APIs públicas de caraguatatuba.sp.leg.br e caragua.leisonline.org:
- 15 vereadores titulares (/vereadores-da-casa), fotos, telefones e e-mails
- Mesa Diretora (/vereadores/mesa-diretora)
- Presenças em sessões plenárias (/api/table-list/data?table=LegislativeAttendances + /api/legislative/attendances/{id}/view)
- Leis, Leis Complementares, Decretos Legislativos e Resoluções 2025–2026 (caragua.leisonline.org)
- Pautas das Sessões Ordinárias (/api/table-list/data?table=LegislativeGuidelines)
"""

import concurrent.futures
import datetime
import html
import http.cookiejar
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
import zlib
from collections import Counter

from common_rx import (
    classify_item,
    compute_bairros_and_rep,
    load_tse_city,
    match_tse_vereador,
    norm,
)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
}
CACHE_DIR = "/tmp/vale_probe"


def fetch_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def fetch_cached_text(cache_name, url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, cache_name)
    if os.path.exists(cpath) and os.path.getsize(cpath) > 100:
        with open(cpath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    raw = fetch_bytes(url)
    txt = raw.decode("utf-8", errors="replace")
    with open(cpath, "w", encoding="utf-8") as f:
        f.write(txt)
    return txt


def strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_leisonline_year(ano):
    cpath = os.path.join(CACHE_DIR, f"car_leis_{ano}.json")
    if os.path.exists(cpath) and os.path.getsize(cpath) > 50:
        with open(cpath, "r", encoding="utf-8") as f:
            return json.load(f)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=SSL_CTX),
    )

    records = []
    seen_nums = set()
    post_data = urllib.parse.urlencode({"bp": "S", "tipo": "0", "numero": "", "ano": str(ano), "assunto": ""}).encode()

    for page_idx in range(1, 25):
        if page_idx == 1:
            req = urllib.request.Request(
                "https://caragua.leisonline.org/leis",
                data=post_data,
                headers={**BROWSER_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            )
        else:
            req = urllib.request.Request(
                f"https://caragua.leisonline.org/leis/{page_idx}",
                headers=BROWSER_HEADERS,
            )
        try:
            with opener.open(req, timeout=25) as r:
                ht = r.read().decode("utf-8", errors="replace")
        except Exception:
            break

        if "<tbody>" not in ht:
            break
        tbody = ht.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        blocks = re.split(r"<tr>\s*<td colspan=3>\s*<hr>\s*</td>\s*</tr>", tbody, flags=re.I)
        if not blocks:
            break

        new_in_page = 0
        for blk in blocks:
            m_tit = re.search(r"<b>([^<]*?n[º°]\s*)</b>\s*([\d\.\s/]+)", blk, re.I)
            if not m_tit:
                continue
            tit = re.sub(r"\s+", " ", strip_tags(m_tit.group(1) + " " + m_tit.group(2))).replace(" / ", "/")
            if not tit or tit in seen_nums:
                continue
            seen_nums.add(tit)
            new_in_page += 1

            m_dt = re.search(r"<b>Publicada:\s*</b>\s*(\d{2})/(\d{2})/(\d{4})", blk, re.I)
            dt_iso = f"{m_dt.group(3)}-{m_dt.group(2)}-{m_dt.group(1)}" if m_dt else f"{ano}-06-01"
            m_aut = re.search(r"<b>Autor:\s*</b>(.*?)</td>", blk, re.S | re.I)
            aut = strip_tags(m_aut.group(1)) if m_aut else ""
            m_em = re.search(r"<p>(.*?)</p>", blk, re.S | re.I)
            em = strip_tags(m_em.group(1)) if m_em else strip_tags(blk)
            m_u = re.search(r"href=['\"](https://caragua\.leisonline\.org/html/[^'\"]+)['\"]", blk, re.I)
            u_link = m_u.group(1) if m_u else "https://caragua.leisonline.org/leis"

            records.append({
                "tit": tit,
                "dt": dt_iso,
                "aut": aut,
                "em": em[:260],
                "u": u_link,
            })

        if new_in_page == 0 or f"/leis/{page_idx + 1}" not in ht:
            break

    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    return records


def collect_caraguatatuba(cfg: dict, out_dir: str) -> dict:
    print(f"\n[CARAGUATATUBA] Coletando {cfg['nome']} ({cfg['cadeiras']} vereadores)...")
    tse_city = load_tse_city("caraguatatuba")
    tse_ver_list = tse_city.get("vereadores", [])

    html_ver = fetch_cached_text(
        "car_vereadores.html",
        "https://caraguatatuba.sp.leg.br/vereadores-da-casa",
    )
    html_mesa = fetch_cached_text(
        "car_mesa.html",
        "https://caraguatatuba.sp.leg.br/vereadores/mesa-diretora",
    )

    mesa_by_name = {}
    for m_blk in re.findall(r'<div class="col-md-4 col-lg-3[^>]*>(.*?)</a\s*>', html_mesa, re.S | re.I):
        t_blk = strip_tags(m_blk)
        for cargo in ("Presidente", "Vice-Presidente", "1º Secretário", "2º Secretário", "1ª Secretária", "2ª Secretária"):
            if cargo.lower() in t_blk.lower():
                nm_clean = re.sub(re.escape(cargo), "", t_blk, flags=re.I).strip()
                mesa_by_name[norm(nm_clean)] = cargo

    cards = re.split(r'<div class="council-card">', html_ver)[1:]

    print(f"  -> Vereadores na Câmara de Caraguatatuba: {len(cards)} (esperado: {cfg['cadeiras']})")
    if len(cards) != cfg["cadeiras"]:
        raise RuntimeError(
            f"[caraguatatuba] Validação de cadeiras falhou: esperado {cfg['cadeiras']}, obtido {len(cards)}"
        )

    fotos_dir = os.path.join(out_dir, "fotos")
    os.makedirs(fotos_dir, exist_ok=True)

    ver_records = []
    for idx_raw, c_html in enumerate(cards, start=1):
        m_img = re.search(r'<img[^>]+src="([^"]+)"', c_html, re.I)
        foto_url = m_img.group(1).strip() if m_img else ""
        if foto_url and foto_url.startswith("/"):
            foto_url = "https://caraguatatuba.sp.leg.br" + foto_url

        m_part = re.search(r'<div class="party-badge[^"]*"[^>]*>(.*?)</div>', c_html, re.S | re.I) or re.search(
            r'<span class="badge[^"]*"[^>]*>(.*?)</span>', c_html, re.S | re.I
        )
        part_raw = strip_tags(m_part.group(1)) if m_part else ""
        part = part_raw.split("-")[0].strip().upper()

        m_nm = re.search(r'<h3 class="card-title-md[^"]*"[^>]*>(.*?)</h3>', c_html, re.S | re.I) or re.search(
            r"<h5[^>]*>(.*?)</h5>", c_html, re.S | re.I
        )
        nome_portal = strip_tags(m_nm.group(1)) if m_nm else f"Vereador {idx_raw}"

        m_mail = re.search(r"mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)", c_html)
        email = m_mail.group(1).strip() if m_mail else ""
        m_tel = re.search(r'tel:([^"]+)', c_html)
        tel = m_tel.group(1).strip() if m_tel else "(12) 3897-2525"
        m_href = re.search(r'href="([^"]*/vereadores-da-casa/[^"]+)"', c_html) or re.search(
            r'href="([^"]*/vereadores/detalhes/[^"]+)"', c_html
        )
        prof_url = m_href.group(1) if m_href else "https://caraguatatuba.sp.leg.br/vereadores-da-casa"
        if prof_url.startswith("/"):
            prof_url = "https://caraguatatuba.sp.leg.br" + prof_url

        tse_m = match_tse_vereador(nome_portal, nome_portal, tse_ver_list)
        ap = tse_m["urna"].title() if tse_m and tse_m.get("urna") else nome_portal
        ap = re.sub(r"\b(De|Da|Do|Dos|Das)\b", lambda m: m.group(1).lower(), ap)

        mesa_role = ""
        for k_nm, crg in mesa_by_name.items():
            if norm(nome_portal) in k_nm or k_nm in norm(nome_portal) or norm(ap) in k_nm:
                mesa_role = crg
                break

        ver_records.append({
            "id": str(idx_raw),
            "ap": ap,
            "nome": tse_m.get("civil", nome_portal).title() if tse_m else nome_portal,
            "part": part or tse_m.get("part24", "S/P"),
            "votos": tse_m.get("votos24", 0),
            "mesa": mesa_role,
            "email": email,
            "tel": tel,
            "url": prof_url,
            "foto_url": foto_url,
            "foto": 0,
            "tse": tse_m,
            "P": 0,
            "A": 0,
            "F": 0,
            "leis_tot": 0,
            "leis_ord": 0,
        })

    ver_records.sort(key=lambda r: norm(r["ap"]))
    for i, r in enumerate(ver_records, start=1):
        r["id"] = str(i)
        fpath = os.path.join(fotos_dir, f"{r['id']}.jpg")
        if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
            r["foto"] = 1
        elif r["foto_url"]:
            try:
                raw_img = fetch_bytes(r["foto_url"], timeout=20)
                if len(raw_img) > 1000:
                    with open(fpath, "wb") as f:
                        f.write(raw_img)
                    r["foto"] = 1
            except Exception:
                pass

    def match_ver_indices(aut_txt):
        na = norm(aut_txt)
        if not na:
            return []
        matched = []
        for idx, v in enumerate(ver_records):
            nap = norm(v["ap"])
            nnm = norm(v["nome"])
            if (nap and nap in na) or (nnm and nnm in na):
                matched.append(idx)
        if not matched:
            for idx, v in enumerate(ver_records):
                toks = [t for t in norm(v["nome"]).split() if len(t) >= 4]
                if len(toks) >= 2 and toks[0] in na and toks[-1] in na:
                    matched.append(idx)
        return sorted(set(matched))

    # 2. Presenças em Sessões Plenárias (2025 e 2026)
    print("  -> Coletando presenças nas sessões plenárias (2025 e 2026)...")
    att_json_txt = fetch_cached_text(
        "car_attendances.json",
        "https://caraguatatuba.sp.leg.br/api/table-list/data?table=LegislativeAttendances&limit=200&page=1&portal_id=1",
    )
    att_data = json.loads(att_json_txt)
    att_ids_25_26 = []
    sess_dates = set()
    for item in att_data.get("data", []):
        c_str = " ".join(str(v) for v in item.values())
        if "2025" in c_str or "2026" in c_str:
            m_aid = re.search(r'data-attendance-id="(\d+)"', c_str)
            if m_aid:
                att_ids_25_26.append(m_aid.group(1))
            for m_dt in re.finditer(r"(\d{2})/(\d{2})/(202[56])", c_str):
                sess_dates.add(f"{m_dt.group(3)}-{m_dt.group(2)}-{m_dt.group(1)}")

    def _fetch_att_view(aid):
        cfile = f"car_att_{aid}.html"
        url = f"https://caraguatatuba.sp.leg.br/api/legislative/attendances/{aid}/view"
        try:
            return fetch_cached_text(cfile, url)
        except Exception:
            return ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        att_htmls = list(ex.map(_fetch_att_view, att_ids_25_26))

    for ht_a in att_htmls:
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", ht_a, re.S | re.I):
            tds = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            if len(tds) >= 2:
                vidxs = match_ver_indices(tds[0])
                status_s = tds[-1].upper()
                for idx_v in vidxs:
                    if status_s == "P" or "PRESENTE" in status_s:
                        ver_records[idx_v]["P"] += 1
                    elif status_s == "F" or ("FALTA" in status_s and "JUST" not in status_s):
                        ver_records[idx_v]["F"] += 1
                    else:
                        ver_records[idx_v]["A"] += 1

    # 3. Leis, Decretos Legislativos e Resoluções 2025–2026 via caragua.leisonline.org
    print("  -> Coletando Leis, Decretos Legislativos e Resoluções (2025 e 2026)...")
    leis_all = fetch_leisonline_year(2025) + fetch_leisonline_year(2026)

    items = []
    catot = {"b": 0, "c": 0, "h": 0, "l": 0, "o": 0}
    v_cats = [{"b": 0, "c": 0, "h": 0, "l": 0, "o": 0} for _ in ver_records]
    coaut_counts = [Counter() for _ in ver_records]
    pref_items = []
    pref_plo = 0
    pref_plc = 0
    prop_seq = 1

    for rec in leis_all:
        tit = rec["tit"]
        dt_iso = rec["dt"]
        aaaammdd = dt_iso.replace("-", "")
        aut = rec["aut"]
        em = rec["em"]

        is_exec = (
            "EXECUTIVO" in aut.upper()
            or "MATEUS VENEZINI" in aut.upper()
            or "PREFEIT" in aut.upper()
        )
        tp_label = "Projeto de Lei Ordinária"
        code_abbr = "LEI"
        if "COMPLEMENTAR" in tit.upper():
            tp_label = "Projeto de Lei Complementar"
            code_abbr = "LC"
        elif "DECRETO LEGISLATIVO" in tit.upper():
            tp_label = "Projeto de Decreto Legislativo"
            code_abbr = "DL"
        elif "RESOLU" in tit.upper():
            tp_label = "Projeto de Resolução"
            code_abbr = "RES"

        if is_exec:
            if code_abbr == "LC":
                pref_plc += 1
            else:
                pref_plo += 1
            pref_items.append([
                "https://caragua.leisonline.org/leis",
                tit,
                em[:240],
                aaaammdd,
                code_abbr,
                "Aprovado / Sancionado",
                "a",
            ])
            continue

        auts = match_ver_indices(aut)
        if not auts and "MESA DIRETORA" in aut.upper():
            auts = [i for i, v in enumerate(ver_records) if v.get("mesa")]
        if not auts:
            continue

        grupo, eh_materia, th = classify_item(tp_label, em)
        for a_i in auts:
            v_cats[a_i][grupo] += 1
            ver_records[a_i]["leis_tot"] += 1
            if code_abbr in ("LEI", "LC"):
                ver_records[a_i]["leis_ord"] += 1

        if len(auts) > 1:
            for i_a in auts:
                for j_a in auts:
                    if i_a != j_a:
                        coaut_counts[i_a][j_a] += 1

        items.append([
            "https://caragua.leisonline.org/leis",
            tit,
            em[:280],
            aaaammdd,
            grupo,
            "Sancionada / Promulgada",
            auts,
            eh_materia,
            th,
            "a",
        ])
        catot[grupo] += 1
        prop_seq += 1

    # 4. Pautas das Sessões Ordinárias (LegislativeGuidelines PDFs)
    print("  -> Coletando Pautas das Sessões Ordinárias (2025 e 2026)...")
    guide_txt = fetch_cached_text(
        "car_guidelines.json",
        "https://caraguatatuba.sp.leg.br/api/table-list/data?table=LegislativeGuidelines&limit=200&page=1&portal_id=1",
    )
    guide_data = json.loads(guide_txt)
    pauta_items = []
    for item in guide_data.get("data", []):
        cols = item.get("columns", [])
        c_str = " ".join(str(c) for c in cols)
        if "/2025" in c_str or "/2026" in c_str:
            m_dt = re.search(r"(\d{2})/(\d{2})/(202[56])", c_str)
            dt_iso = f"{m_dt.group(3)}-{m_dt.group(2)}-{m_dt.group(1)}" if m_dt else "2025-06-01"
            for act in item.get("actions", []):
                if act.get("link") and ".pdf" in act.get("link", "").lower():
                    pauta_items.append((item["id"], dt_iso, act["link"]))
                    break

    def _dl_pauta_pdf(args):
        gid, dt_iso, url = args
        cpath = os.path.join(CACHE_DIR, f"car_pauta_{gid}.pdf")
        if os.path.exists(cpath) and os.path.getsize(cpath) > 500:
            with open(cpath, "rb") as f:
                return dt_iso, url, f.read()
        try:
            raw = fetch_bytes(url, timeout=20)
            if len(raw) > 500:
                with open(cpath, "wb") as f:
                    f.write(raw)
                return dt_iso, url, raw
        except Exception:
            pass
        return dt_iso, url, b""

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        pauta_pdfs = list(ex.map(_dl_pauta_pdf, pauta_items))

    vetos_list = []
    for dt_sess, pdf_url, pdf_bytes in pauta_pdfs:
        if not pdf_bytes:
            continue
        streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S)
        txt_parts = []
        for s in streams:
            try:
                dec = zlib.decompress(s).decode("latin1", errors="ignore")
                for m_str in re.findall(r"\(([^()]*)\)", dec):
                    txt_parts.append(m_str)
            except Exception:
                pass
        full_pauta = re.sub(r"\s+", " ", "".join(txt_parts))
        if len(full_pauta) < 40:
            continue

        for m_pr in re.finditer(
            r"(VETO(?:\s+TOTAL|\s+PARCIAL)?)\s*N[º°o\.]*\s*(\d+/\d{2,4})(.*?)(?=PROJETO DE|VETO|\d+\.\s*PROJETO|$)",
            full_pauta,
            re.I,
        ):
            tp_p = m_pr.group(1).strip().title()
            num_p = m_pr.group(2).strip()
            desc_p = m_pr.group(3).strip()[:240]
            vetos_list.append({
                "t": f"{tp_p} {num_p}",
                "d": dt_sess,
                "e": desc_p,
                "aut": "Poder Executivo",
                "status": "Apreciado",
                "s": 0,
                "n": 0,
                "a": 0,
                "u": pdf_url,
                "vs": [],
                "vn": [],
                "va": [],
            })

    items.sort(key=lambda x: x[3], reverse=True)
    sess_list = [{"d": d} for d in sorted(sess_dates)]
    ult_sessao = sess_list[-1]["d"] if sess_list else "2026-11-12"

    n_ver = len(ver_records)
    vmat = [[[0, 0] for _ in range(n_ver)] for _ in range(n_ver)]
    v_out = []
    for idx, r in enumerate(ver_records):
        tse_m = r["tse"]
        top_parc = [[other_i, cnt] for other_i, cnt in coaut_counts[idx].most_common(3)]
        v_out.append({
            "id": r["id"],
            "nome": r["nome"],
            "ap": r["ap"],
            "part": r["part"],
            "votos": r["votos"],
            "ini": "2025-01-01",
            "mesa": r["mesa"],
            "P": r["P"],
            "A": r["A"],
            "F": r["F"],
            "email": r["email"],
            "tel": r["tel"],
            "whats": "",
            "vs": 0,
            "vn": 0,
            "va": 0,
            "naos": [],
            "cat": v_cats[idx],
            "url": r["url"],
            "foto": r["foto"],
            "foto_url": r["foto_url"],
            "mand": 1,
            "desde": 2025,
            "mand_txt": "Mandato 2025–2028",
            "mand_hist": ["2025–2028"],
            "nota_exec": "",
            "leis_tot": r["leis_tot"],
            "leis_ord": r["leis_ord"],
            "leis_det": {"Normas em vigor": r["leis_tot"]},
            "coms": [],
            "em_imp": [],
            "alin": 0,
            "alin_s": 0,
            "alin_t": 0,
            "parc": top_parc,
            "cob_pend": 0,
            "rec24": tse_m.get("rec24", 0.0),
            "gasto24": tse_m.get("gasto24", 0.0),
            "cpv24": tse_m.get("cpv24", 0.0),
            "gasto20": tse_m.get("gasto20", 0.0),
            "outras_cand": tse_m.get("outras", []),
            "hist_tse": tse_m.get("hist", []),
        })

    bairros_out, bairrosn_out, rep_out = compute_bairros_and_rep(items, cfg["bairros"])
    p_tse = tse_city.get("prefeito") or {}
    p_cfg = cfg["pref"]
    now_br = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))

    return {
        "ger": now_br.strftime("%Y-%m-%d"),
        "gerh": now_br.strftime("%Y-%m-%dT%H:%M-03:00"),
        "posse": "2025-01-01",
        "cfg": {
            "nome": cfg["nome"],
            "uf": cfg["uf"],
            "portal_url": cfg["portal_url"],
            "proc_url": cfg["proc_url"],
            "form_url": cfg["form_url"],
            "leis_url": cfg["leis_url"],
            "fonte_nome": cfg["fonte_nome"],
            "endereco": cfg["endereco"],
        },
        "rep": rep_out,
        "ult": ult_sessao,
        "sess": sess_list,
        "semreg": [],
        "nvot": 0,
        "unan": 0,
        "v": v_out,
        "items": items,
        "catot": catot,
        "bairros": bairros_out,
        "bairrosn": bairrosn_out,
        "sal": cfg["sal"],
        "pref": {
            "nome": p_cfg["nome"],
            "civil": p_cfg["civil"],
            "part": p_cfg["part"],
            "vice": p_cfg["vice"],
            "votos_1t": p_tse.get("votos_1t", 0),
            "votos_2t": p_tse.get("votos_2t", 0),
            "pct_1t": p_tse.get("pct_1t", ""),
            "pct_2t": p_tse.get("pct_2t", ""),
            "posse": "2025-01-01",
            "sal": p_cfg["sal"],
            "tot": len(pref_items),
            "plo": pref_plo,
            "plc": pref_plc,
            "ret": 0,
            "aprov": len(pref_items),
            "tram": 0,
            "rej": 0,
            "items": pref_items,
            "vetos_tot": len(vetos_list),
            "vetos_mant": len(vetos_list),
            "vetos_derr": 0,
            "vetos": vetos_list,
            "cob_tot": 0,
            "cob_pend": 0,
            "cob_resp": 0,
            "rec24": p_tse.get("rec24", 0.0),
            "gasto24": p_tse.get("gasto24", 0.0),
            "cpv24": p_tse.get("cpv24", 0.0),
            "outras_cand": p_tse.get("outras", []),
            "hist_tse": p_tse.get("hist", []),
        },
        "rachas": [],
        "vmat": vmat,
    }
