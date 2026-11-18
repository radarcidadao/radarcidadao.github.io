#!/usr/bin/env python3
"""
Motor coletor para a Câmara Municipal de Jacareí (13 vereadores).
Consome páginas públicas de Transparência Legislativa de jacarei.sp.leg.br:
- Vereadores em exercício (2025–2028), contatos, fotos e Mesa Diretora
- Pedidos de Informação (2025 e 2026), Requerimentos (2026)
- Projetos do Legislativo (PLL, PDL) e do Executivo (PLE, Vetos)
- Atas Eletrônicas das Sessões Ordinárias (.docx) para presença nominal, Indicações e Moções
"""

import concurrent.futures
import datetime
import html
import io
import os
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
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

UA = "Mozilla/5.0 (compatible; RadarCidadao/1.0; +https://radarcidadao.github.io)"
CACHE_DIR = "/tmp/vale_probe"


def safe_url(url):
    u = url.strip().replace("http://www.jacarei.sp.leg.br", "https://www.jacarei.sp.leg.br")
    p = urllib.parse.urlsplit(u)
    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/%:@")
    query = urllib.parse.quote(urllib.parse.unquote(p.query), safe="=&%:@?/")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


def fetch_bytes(url, timeout=35):
    req = urllib.request.Request(
        safe_url(url),
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def fetch_cached_html(cache_name, url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, cache_name)
    if os.path.exists(cpath) and os.path.getsize(cpath) > 200:
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


def collect_jacarei(cfg: dict, out_dir: str) -> dict:
    print(f"\n[JACAREÍ] Coletando {cfg['nome']} ({cfg['cadeiras']} vereadores)...")
    tse_city = load_tse_city("jacarei")
    tse_ver_list = tse_city.get("vereadores", [])

    html_ver = fetch_cached_html(
        "jac_vereadores.html",
        "https://www.jacarei.sp.leg.br/vereadores-2025-a-2028/",
    )
    items_ver = re.findall(
        r'<div class="fg-item[^"]*">(.*?)</figcaption>',
        html_ver,
        re.S | re.I,
    )
    print(f"  -> Vereadores na galeria oficial: {len(items_ver)} (esperado: {cfg['cadeiras']})")
    if len(items_ver) != cfg["cadeiras"]:
        raise RuntimeError(
            f"[jacarei] Validação de cadeiras falhou: esperado {cfg['cadeiras']}, obtido {len(items_ver)}"
        )

    fotos_dir = os.path.join(out_dir, "fotos")
    os.makedirs(fotos_dir, exist_ok=True)

    ver_records = []
    for idx_raw, block in enumerate(items_ver, start=1):
        m_href = re.search(r'href="([^"]+)"', block)
        prof_url = m_href.group(1) if m_href else "https://www.jacarei.sp.leg.br/vereadores-2025-a-2028/"
        m_img = re.search(r'data-src-fg="([^"]+)"', block) or re.search(r'src="([^"]+)"', block)
        foto_url = m_img.group(1) if m_img else ""
        m_cap = re.search(r'<div class="fg-caption-title">(.*?)</div>', block, re.S)
        cap_raw = strip_tags(m_cap.group(1)) if m_cap else f"Vereador {idx_raw}"

        mesa = ""
        if " - " in cap_raw:
            cap_main, mesa_part = cap_raw.split(" - ", 1)
            mesa = mesa_part.strip()
        else:
            cap_main = cap_raw.strip()

        m_part = re.search(r"\(([^)]+)\)", cap_main)
        part = m_part.group(1).strip().upper() if m_part else ""
        ap = re.sub(r"\s*\([^)]+\)", "", cap_main).strip()

        slug_v = prof_url.rstrip("/").split("/")[-1]
        prof_html = ""
        try:
            prof_html = fetch_cached_html(f"jac_ver_{slug_v}.html", prof_url)
        except Exception:
            pass

        m_mail = re.search(r"mailto:([a-zA-Z0-9._%+-]+@jacarei\.sp\.leg\.br)", prof_html)
        email = m_mail.group(1).strip() if m_mail else ""
        m_tel = re.search(r"Telefones?:\s*([^\n<]+)", strip_tags(prof_html))
        tel = m_tel.group(1).strip()[:45] if m_tel else "(12) 3955-2200"

        tse_m = match_tse_vereador(ap, ap, tse_ver_list)
        if not part and tse_m.get("part24"):
            part = tse_m["part24"]

        ver_records.append({
            "id": str(idx_raw),
            "ap": ap,
            "nome": tse_m.get("civil", ap).title() if tse_m else ap,
            "part": part or "S/P",
            "votos": tse_m.get("votos24", 0),
            "mesa": mesa,
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
            nap_short = re.sub(r"^(dr|dra|prof|professor|pastor)\s+", "", nap).strip()
            if (nap and nap in na) or (nnm and nnm in na) or (len(nap_short) >= 5 and nap_short in na):
                matched.append(idx)
        if not matched:
            for idx, v in enumerate(ver_records):
                toks = [t for t in norm(v["ap"]).split() if len(t) >= 4]
                if toks and all(t in na for t in toks[:2]):
                    matched.append(idx)
        return sorted(set(matched))

    items = []
    catot = {"b": 0, "c": 0, "h": 0, "l": 0, "o": 0}
    v_cats = [{"b": 0, "c": 0, "h": 0, "l": 0, "o": 0} for _ in ver_records]
    v_cob_pend = [0 for _ in ver_records]
    coaut_counts = [Counter() for _ in ver_records]
    cob_tot_all = 0
    cob_pend_all = 0
    cob_resp_all = 0
    prop_seq = 1

    def add_item(auts, code_abbr, num_s, aaaammdd, tp_full, ass, res_label="", st_code="a", link_u=""):
        nonlocal prop_seq, cob_tot_all, cob_pend_all, cob_resp_all
        if not auts or not ass:
            return
        grupo, eh_materia, th = classify_item(tp_full, ass)
        if grupo == "c":
            cob_tot_all += 1
            if st_code == "p":
                cob_pend_all += 1
                for a_i in auts:
                    v_cob_pend[a_i] += 1
            elif st_code == "a":
                cob_resp_all += 1

        if len(auts) > 1:
            for i_a in auts:
                for j_a in auts:
                    if i_a != j_a:
                        coaut_counts[i_a][j_a] += 1

        pid = link_u if link_u else str(prop_seq)
        prop_seq += 1
        items.append([
            pid,
            f"{code_abbr} {num_s}",
            ass[:280],
            aaaammdd,
            grupo,
            res_label,
            auts,
            eh_materia,
            th,
            st_code,
        ])
        catot[grupo] += 1
        for a_i in auts:
            v_cats[a_i][grupo] += 1

    # 2. Pedidos de Informação (2025 e 2026) e Requerimentos (2026)
    print("  -> Coletando Pedidos de Informação e Requerimentos...")
    for ano, cfile, url, code_abbr, tp_label in [
        (
            2025,
            "jac_ped_2025.html",
            "https://www.jacarei.sp.leg.br/pedidos-de-informacao/todos-os-pedidos-de-informacoes-2025/",
            "PI",
            "Pedido de Informações",
        ),
        (
            2026,
            "jac_ped_2026.html",
            "https://www.jacarei.sp.leg.br/pedidos-de-informacao/todos-os-pedidos-de-informacoes-2026/",
            "PI",
            "Pedido de Informações",
        ),
        (
            2026,
            "jac_req_2026.html",
            "https://www.jacarei.sp.leg.br/requerimentos/requerimentos-2026/",
            "REQ",
            "Requerimento",
        ),
    ]:
        try:
            ht = fetch_cached_html(cfile, url)
            m_art = re.search(r"<article[^>]*>(.*?)</article>", ht, re.S | re.I)
            art_html = m_art.group(1) if m_art else ht
            for p_blk in re.split(r"<hr\s*/?>", art_html, flags=re.I):
                txt_b = strip_tags(p_blk)
                if "Autoria" not in txt_b or "Assunto" not in txt_b:
                    continue
                m_num = re.search(r"(\d{1,4}/\d{4})", txt_b)
                num_s = m_num.group(1) if m_num else f"S/N/{ano}"
                m_aut = re.search(r"Autoria\s*[–\-:]\s*(.*?)\s+Assunto\s*[–\-:]\s*(.*?)(?:\s+Resposta|$)", txt_b)
                if not m_aut:
                    continue
                aut_s, ass_s = m_aut.group(1).strip(), m_aut.group(2).strip()
                m_href = re.search(r'href="([^"]+\.pdf)"', p_blk, re.I)
                pdf_u = m_href.group(1) if m_href else url
                auts = match_ver_indices(aut_s)
                if auts:
                    has_resp = "resposta" in p_blk.lower() and "aguardando" not in p_blk.lower()
                    add_item(
                        auts,
                        code_abbr,
                        num_s,
                        f"{ano}0615",
                        tp_label,
                        ass_s,
                        "Respondido" if has_resp else "Aguardando Prefeitura",
                        "a" if has_resp else "p",
                        pdf_u,
                    )
        except Exception as e:
            print(f"    [Aviso] Falha parcial em {cfile}: {e}")

    # 3. Projetos de Lei (PLL, PLE), Decretos Legislativos (PDL) e Vetos (VT)
    print("  -> Coletando Projetos de Lei, Decretos Legislativos e Vetos (2025 e 2026)...")
    proj_pages = [
        (2025, "PLL", "Projeto de Lei Ordinária", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-pll-2/"),
        (2025, "PLL2", "Projeto de Lei Ordinária", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-pll-parte-2/"),
        (2025, "PLCL", "Projeto de Lei Complementar", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-plcl/"),
        (2026, "PLL", "Projeto de Lei Ordinária", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2026-pll/"),
        (2026, "PLCL", "Projeto de Lei Complementar", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2026-plcl/"),
        (2025, "PLE", "Projeto de Lei Ordinária", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-ple/"),
        (2025, "PLCE", "Projeto de Lei Complementar", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projeto-de-lei-complementar-do-executivo-2025/"),
        (2026, "PLE", "Projeto de Lei Ordinária", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2026-ple/"),
        (2026, "PLCE", "Projeto de Lei Complementar", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2026-plce/"),
        (2025, "PDL", "Projeto de Decreto Legislativo", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-pdl/"),
        (2025, "PR", "Projeto de Resolução", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-pr/"),
        (2025, "VT", "Veto", "https://www.jacarei.sp.leg.br/trabalhos-gerais/projetos-ano-2025-vt/"),
    ]

    pref_items = []
    pref_plo = 0
    pref_aprov = 0
    pref_tram = 0
    pref_rej = 0
    vetos_list = []
    vetos_mant = 0
    vetos_derr = 0

    for ano, cod_tp, label_tp, url in proj_pages:
        cfile = f"jac_tg_{cod_tp.lower()}_{ano}.html"
        try:
            ht = fetch_cached_html(cfile, url)
        except Exception:
            continue
        m_art = re.search(r"<article[^>]*>(.*?)</article>", ht, re.S | re.I)
        art_html = m_art.group(1) if m_art else ht
        blocks = re.split(r"<h[23][^>]*>", art_html, flags=re.I)[1:]
        for blk in blocks:
            txt = strip_tags("<h2>" + blk)
            if "Ementa" not in txt and "Autor" not in txt:
                continue
            m_num = re.search(r"n[º°]\s*(\d+/\d{4})", txt, re.I)
            num_s = m_num.group(1) if m_num else f"S/N/{ano}"
            m_em = re.search(r"Ementa\s*[–\-:]\s*(.*?)(?:Autor(?:ia)?\s*[–\-:]|\d{2}/\d{2}/\d{4}|$)", txt, re.I)
            ementa = m_em.group(1).strip() if m_em else txt[:220]
            m_aut = re.search(r"Autor(?:ia)?\s*[–\-:]\s*(.*?)(?:\d{2}/\d{2}/\d{4}|Protocolo|Data|Boletim|Parecer|$)", txt, re.I)
            aut_s = m_aut.group(1).strip().rstrip(".") if m_aut else ""
            m_dt = re.search(r"(\d{2})/(\d{2})/(\d{4})", txt)
            dt_iso = f"{m_dt.group(3)}-{m_dt.group(2)}-{m_dt.group(1)}" if m_dt else f"{ano}-05-01"
            aaaammdd = dt_iso.replace("-", "")
            m_href = re.search(r'href="([^"]+\.pdf)"', blk, re.I)
            pdf_u = m_href.group(1) if m_href else url

            res_label = "Em tramitação"
            st_code = "t"
            if re.search(r"Projeto Aprovado|Veto Mantido|Promulgad|Sancionad|Lei n", txt, re.I):
                res_label = "Aprovado"
                st_code = "a"
            elif re.search(r"Projeto Rejeitado|Veto Rejeitado", txt, re.I):
                res_label = "Rejeitado"
                st_code = "r"
            elif re.search(r"Projeto Retirado|Arquivad", txt, re.I):
                res_label = "Retirado"
                st_code = "r"

            base_tp = "PLL" if cod_tp == "PLL2" else cod_tp
            if base_tp in ("PLE", "PLCE"):
                pref_plo += 1
                if st_code == "a":
                    pref_aprov += 1
                elif st_code == "r":
                    pref_rej += 1
                else:
                    pref_tram += 1
                pref_items.append([pdf_u, f"{base_tp} {num_s}", ementa[:240], aaaammdd, base_tp, res_label, st_code])
            elif base_tp == "VT":
                is_der = bool(re.search(r"Veto Rejeitado|Derrubad", txt, re.I))
                is_man = bool(re.search(r"Veto Mantido", txt, re.I))
                if is_der:
                    vetos_derr += 1
                elif is_man:
                    vetos_mant += 1
                vetos_list.append({
                    "t": f"Veto {num_s}",
                    "d": dt_iso,
                    "e": ementa[:240],
                    "aut": aut_s or "Poder Executivo",
                    "status": "Derrubado" if is_der else ("Mantido" if is_man else "Em tramitação"),
                    "s": 0,
                    "n": 0,
                    "a": 0,
                    "u": pdf_u,
                    "vs": [],
                    "vn": [],
                    "va": [],
                })
            else:
                auts = match_ver_indices(aut_s)
                if auts:
                    add_item(auts, base_tp, num_s, aaaammdd, label_tp, ementa, res_label, st_code, pdf_u)
                    if st_code == "a":
                        for a_i in auts:
                            ver_records[a_i]["leis_tot"] += 1
                            if base_tp in ("PLL", "PLCL"):
                                ver_records[a_i]["leis_ord"] += 1

    # 4. Atas Eletrônicas DOCX -> Presença, Indicações e Moções
    print("  -> Processando Atas Eletrônicas DOCX (2025 e 2026)...")
    atas_html = fetch_cached_html(
        "jac_atas.html",
        "https://www.jacarei.sp.leg.br/atas-eletronicas-das-sessoes/",
    )
    docx_urls = re.findall(r'href="(https?://www\.jacarei\.sp\.leg\.br/wp-content/uploads/202[56]/[^"]+\.docx)"', atas_html)
    docx_urls = list(dict.fromkeys([u for u in docx_urls if "SO" in u or "Ordin" in u or "2025" in u or "2026" in u]))

    def _download_docx(u):
        fn = "jac_ata_" + re.sub(r"[^a-zA-Z0-9._-]", "_", u.split("/")[-1])
        cpath = os.path.join(CACHE_DIR, fn)
        if os.path.exists(cpath) and os.path.getsize(cpath) > 1000:
            with open(cpath, "rb") as f:
                return u, f.read()
        try:
            raw = fetch_bytes(u, timeout=25)
            if len(raw) > 1000:
                with open(cpath, "wb") as f:
                    f.write(raw)
                return u, raw
        except Exception:
            pass
        return u, b""

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        docx_payloads = list(ex.map(_download_docx, docx_urls))

    sess_dates = set()
    nvot = 0
    unan = 0

    for u, raw_bytes in docx_payloads:
        if not raw_bytes:
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
            xml_doc = zf.read("word/document.xml")
            root = ET.fromstring(xml_doc)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paras = []
            for p_el in root.findall(".//w:p", ns):
                t = "".join(x.text for x in p_el.findall(".//w:t", ns) if x.text).strip()
                if t:
                    paras.append(re.sub(r"\s+", " ", t))
        except Exception:
            continue

        full_head = " ".join(paras[:18])
        m_dt = re.search(r"(\d{2})/(\d{2})/(202[56])", full_head)
        if m_dt:
            dt_sess = f"{m_dt.group(3)}-{m_dt.group(2)}-{m_dt.group(1)}"
        else:
            ano_u = "2026" if "/2026/" in u else "2025"
            dt_sess = f"{ano_u}-06-01"
        aaaammdd = dt_sess.replace("-", "")

        for p_txt in paras[:30]:
            if "compareceram" in p_txt.lower() and "vereadores" in p_txt.lower():
                present_idxs = set(match_ver_indices(p_txt))
                if len(present_idxs) >= 6:
                    sess_dates.add(dt_sess)
                    for idx_v, v in enumerate(ver_records):
                        if idx_v in present_idxs:
                            v["P"] += 1
                        else:
                            v["A"] += 1
                break

        ano_sess = dt_sess[:4]
        for p_txt in paras:
            # 1) Ordem do Dia: votação nominal de projetos/vetos
            if re.search(r"vota[çc][ãa]o nominal.*?(?:APROVAD|REJEITAD|MANTID)", p_txt, re.I):
                nvot += 1
                if not re.search(r"VOTOS?\s+CONTR[ÁA]RIOS?|CONTRA\b", p_txt, re.I):
                    unan += 1
                continue

            # 2) Votos de Pesar: "VOTOS DE PESAR ... formulado pelo(a) vereador(a) X"
            if p_txt.upper().startswith("VOTOS DE PESAR"):
                for seg in p_txt.split(";"):
                    m_vp = re.search(r"falecimentos? de:?\s*(.*?),\s*formulado pel[oa]s?\s+vereador(?:a|es)?\s+(.*)", seg, re.I)
                    if not m_vp:
                        m_vp = re.search(r"^\s*(.*?),\s*formulado pel[oa]s?\s+vereador(?:a|es)?\s+(.*)", seg, re.I)
                    if m_vp:
                        homenageado = m_vp.group(1).strip()
                        auts = match_ver_indices(m_vp.group(2))
                        if auts and homenageado:
                            add_item(
                                auts,
                                "PESAR",
                                f"S/N/{ano_sess}",
                                aaaammdd,
                                "Moção",
                                f"Voto de pesar pelo falecimento de {homenageado}.",
                                "Apresentado em Plenário",
                                "a",
                                u,
                            )
                continue

            # 3) Fase do Expediente: "<NOME DO VEREADOR> - Indicações: ... Moções: 0005 - Moção..."
            m_head = re.match(r"^([A-ZÀ-Ú\s\(\)]{4,45}?)\s*[\-–]\s*(Indica[çc][õo]es?:|Mo[çc][õo]es?:|Requerimentos?:|Pedidos? de Informa)", p_txt)
            if m_head:
                auts = match_ver_indices(m_head.group(1))
                if not auts:
                    continue
                # Extrair Moções com texto completo
                m_moc_sec = re.search(r"Mo[çc][õo]es?:\s*(.*?)(?:Requerimentos?:|Pedidos? de Informa[çc][õo]es?:|$)", p_txt, re.I)
                if m_moc_sec:
                    moc_blob = m_moc_sec.group(1).strip()
                    for m_m in re.finditer(r"(\d{1,4})\s*[\-–]\s*(Mo[çc][ãa]o.*?)(?=\s+\d{4}\s*[\-–]\s*Mo[çc][ãa]o|$)", moc_blob, re.I):
                        num_m = f"{int(m_m.group(1))}/{ano_sess}"
                        txt_m = m_m.group(2).strip().rstrip(".")
                        add_item(auts, "MOC", num_m, aaaammdd, "Moção", txt_m, "Aprovado", "a", u)
                # Extrair Indicações numeradas na sessão
                m_ind_sec = re.search(r"Indica[çc][õo]es?:\s*(.*?)(?:Mo[çc][õo]es?:|Requerimentos?:|Pedidos? de Informa|$)", p_txt, re.I)
                if m_ind_sec:
                    ind_nums = re.findall(r"\b(\d{1,4})\b", m_ind_sec.group(1))
                    for n_i in ind_nums:
                        num_i = f"{int(n_i)}/{ano_sess}"
                        add_item(
                            auts,
                            "IND",
                            num_i,
                            aaaammdd,
                            "Indicação",
                            f"Indicação nº {num_i} encaminhada à Prefeitura Municipal na Sessão Ordinária de {dt_sess[8:10]}/{dt_sess[5:7]}/{ano_sess}.",
                            "Encaminhada ao Executivo",
                            "a",
                            u,
                        )

    # Ordenar items da mais recente para a mais antiga
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
            "leis_det": {"Normas aprovadas": r["leis_tot"]},
            "coms": [],
            "em_imp": [],
            "alin": 0,
            "alin_s": 0,
            "alin_t": 0,
            "parc": top_parc,
            "cob_pend": v_cob_pend[idx],
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
        "nvot": nvot,
        "unan": unan,
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
            "plc": 0,
            "ret": 0,
            "aprov": pref_aprov,
            "tram": pref_tram,
            "rej": pref_rej,
            "items": pref_items,
            "vetos_tot": len(vetos_list),
            "vetos_mant": vetos_mant,
            "vetos_derr": vetos_derr,
            "vetos": vetos_list,
            "cob_tot": cob_tot_all,
            "cob_pend": cob_pend_all,
            "cob_resp": cob_resp_all,
            "rec24": p_tse.get("rec24", 0.0),
            "gasto24": p_tse.get("gasto24", 0.0),
            "cpv24": p_tse.get("cpv24", 0.0),
            "outras_cand": p_tse.get("outras", []),
            "hist_tse": p_tse.get("hist", []),
        },
        "rachas": [],
        "vmat": vmat,
    }
