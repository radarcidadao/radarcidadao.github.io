#!/usr/bin/env python3
"""
Motor coletor para a Câmara Municipal de São Paulo (CMSP — 55 vereadores).
Consome APIs oficiais abertas:
- WordPress REST API (55 vereadores ativos em exercício, fotos, contatos, Mesa Diretora)
- SPLegis WS2 JSON (proposições 2025/2026, autores, deliberações, comissões, leis sancionadas)
- Azure Blob SIP XML (PRESENCAS_2025/2026.xml e VOTACOES_2025/2026.xml)
"""

import concurrent.futures
import datetime
import html
import json
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

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
CACHE_DIR = "/tmp/cmsp_probe"


def fetch_bytes(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def fetch_cached_or_url(cache_name, url, is_json=True):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, cache_name)
    if os.path.exists(cpath) and os.path.getsize(cpath) > 50:
        with open(cpath, "rb") as f:
            raw = f.read()
    else:
        raw = fetch_bytes(url)
        with open(cpath, "wb") as f:
            f.write(raw)
    if is_json:
        return json.loads(raw.decode("utf-8", errors="replace"))
    return raw


def parse_date_iso(d_str):
    if not d_str:
        return "2025-01-01"
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", str(d_str).strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(d_str).strip())
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return "2025-01-01"


def collect_sp(cfg: dict, out_dir: str) -> dict:
    print(f"\n[CMSP] Coletando {cfg['nome']} ({cfg['cadeiras']} vereadores)...")
    tse_city = load_tse_city("sp")
    tse_ver_list = tse_city.get("vereadores", [])

    # 1. 55 Vereadores Ativos via WP-JSON
    p1 = fetch_cached_or_url(
        "wp_vereadores_p1.json",
        "https://www.saopaulo.sp.leg.br/wp-json/wp/v2/vereador?per_page=100&page=1",
        is_json=True,
    )
    p2 = fetch_cached_or_url(
        "wp_vereadores_p2.json",
        "https://www.saopaulo.sp.leg.br/wp-json/wp/v2/vereador?per_page=100&page=2",
        is_json=True,
    )
    all_wp = (p1 if isinstance(p1, list) else []) + (p2 if isinstance(p2, list) else [])
    ativos_wp = [
        x for x in all_wp
        if isinstance(x, dict) and x.get("meta_all", {}).get("_cmsp_vereador_ativo") == ["on"]
    ]
    print(f"  -> Vereadores ativos na CMSP: {len(ativos_wp)} (esperado: {cfg['cadeiras']})")
    if len(ativos_wp) != cfg["cadeiras"]:
        raise RuntimeError(
            f"[sp] Validação de cadeiras falhou: esperado {cfg['cadeiras']}, obtido {len(ativos_wp)}"
        )

    # 2. Dados SPLegis de vereadores (comissões e mandatos)
    splegis_ver_raw = fetch_cached_or_url(
        "vereadores_json.json",
        "https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/VereadoresCMSPJSON",
        is_json=True,
    )
    splegis_by_id = {}
    sp_arr = (
        splegis_ver_raw.get("vereadores", [])
        if isinstance(splegis_ver_raw, dict)
        else (
            splegis_ver_raw[0].get("vereadores", [])
            if (splegis_ver_raw and isinstance(splegis_ver_raw[0], dict) and "vereadores" in splegis_ver_raw[0])
            else (splegis_ver_raw if isinstance(splegis_ver_raw, list) else [])
        )
    )
    for v in sp_arr:
        if isinstance(v, dict):
            splegis_by_id[int(v.get("chave", 0))] = v

    fotos_dir = os.path.join(out_dir, "fotos")
    os.makedirs(fotos_dir, exist_ok=True)

    ver_records = []
    for x in ativos_wp:
        m = x.get("meta_all", {})
        sp_id_raw = (m.get("_cmsp_vereador_consulta_splegis_id") or ["0"])[0]
        vid = int(sp_id_raw) if str(sp_id_raw).isdigit() else int(x["id"])
        ap = html.unescape((m.get("_cmsp_vereador_name") or [x.get("title", {}).get("rendered", "")])[0]).strip()
        nome_civil = html.unescape((m.get("_cmsp_vereador_nome_completo") or [ap])[0]).strip()
        part = (m.get("_cmsp_vereador_party") or [""])[0].strip().upper()
        email = (m.get("_cmsp_vereador_contato_email") or [""])[0].strip()
        ramal = (m.get("_cmsp_vereador_contato_ramal") or [""])[0].strip()
        tel = f"(11) 3396-{ramal}" if ramal and len(ramal) == 4 else "(11) 3396-4000"
        foto_url = (m.get("_cmsp_vereador_image") or [""])[0].strip()
        mesa_flag = (m.get("_cmsp_vereador_mesa-diretora") or [""])[0] == "on"
        mesa_pos = (m.get("_cmsp_vereador_mesa-diretora-posicao") or [""])[0].strip()

        sp_info = splegis_by_id.get(vid, {})
        coms = []
        seen_coms = set()
        for cg in sp_info.get("cargos", []):
            ini = str(cg.get("inicio", ""))
            fim = str(cg.get("fim", ""))
            if any(y in ini or y in fim for y in ("2025", "2026")):
                orgao = re.sub(r"^Comissão\s+(?:Permanente\s+|Extraordinária\s+)?de\s+", "", cg.get("orgao", "").strip(), flags=re.I).title()
                cargo_nm = cg.get("cargo", "Membro").strip().title()
                if orgao and "Mesa Diretora" not in orgao:
                    key = norm(orgao)
                    if key not in seen_coms:
                        seen_coms.add(key)
                        coms.append({"c": cargo_nm, "n": orgao})

        mand_years = []
        for md in sp_info.get("mandatos", []):
            y0 = str(md.get("inicio", ""))[:4]
            if y0.isdigit():
                mand_years.append(int(y0))
        mand_count = max(1, len(set(mand_years)))
        desde_ano = min(mand_years) if mand_years else 2025
        mand_txt = f"{mand_count}º mandato · desde {desde_ano}"

        tse_m = match_tse_vereador(ap, nome_civil, tse_ver_list)
        if not part and tse_m.get("part24"):
            part = tse_m["part24"]

        ver_records.append({
            "vid": vid,
            "id": str(vid),
            "ap": ap,
            "nome": tse_m.get("civil", nome_civil).title() if tse_m else nome_civil,
            "part": part or "S/P",
            "votos": tse_m.get("votos24", 0),
            "mesa": mesa_pos if (mesa_flag and mesa_pos) else "",
            "email": email,
            "tel": tel,
            "url": x.get("link", "https://www.saopaulo.sp.leg.br/vereadores/"),
            "foto_url": foto_url,
            "foto": 1 if foto_url else 0,
            "coms": coms[:5],
            "mand": mand_count,
            "desde": desde_ano,
            "mand_txt": mand_txt,
            "mand_hist": [f"{y}–{y+3}" for y in sorted(set(mand_years))] if mand_years else ["2025–2028"],
            "tse": tse_m,
            "P": 0,
            "A": 0,
            "F": 0,
            "leis_tot": 0,
            "leis_ord": 0,
        })

    # Ordenar alfabeticamente por apelido parlamentar (padrão Radar Taubaté)
    ver_records.sort(key=lambda r: norm(r["ap"]))
    vid_to_idx = {r["vid"]: i for i, r in enumerate(ver_records)}

    # Baixar fotos localmente em paralelo
    def _dl_foto(r):
        fpath = os.path.join(fotos_dir, f"{r['id']}.jpg")
        if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
            r["foto"] = 1
            return
        if not r.get("foto_url"):
            return
        try:
            raw = fetch_bytes(r["foto_url"], timeout=20)
            if len(raw) > 1000:
                with open(fpath, "wb") as f:
                    f.write(raw)
                r["foto"] = 1
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_dl_foto, ver_records))

    # 3. Leis sancionadas de autoria dos vereadores (todas as legislaturas e recorte)
    def _fetch_leis_ver(r):
        vid = r["vid"]
        cfile = f"leis_ver_{vid}.json"
        url = f"https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/LeisAprovadasPorPromoventeJSON?Codigo={vid}"
        try:
            data = fetch_cached_or_url(cfile, url, is_json=True)
            arr = data if isinstance(data, list) else []
            r["leis_tot"] = len(arr)
            r["leis_ord"] = sum(1 for item in arr if str(item.get("sigla_projeto", "PL")).upper() in ("PL", "PLO"))
        except Exception:
            pass

    print("  -> Consultando leis sancionadas por vereador...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_fetch_leis_ver, ver_records))

    # 4. Presenças em Plenário (PRESENCAS_2025.xml e PRESENCAS_2026.xml)
    print("  -> Processando presenças em plenário (2025 e 2026)...")
    sess_dates = set()
    for ano in (2025, 2026):
        xml_bytes = fetch_cached_or_url(
            f"pres_{ano}_xml.dat",
            f"https://splegispdarmazenamento.blob.core.windows.net/containersip/PRESENCAS_{ano}.xml",
            is_json=False,
        )
        root = ET.fromstring(xml_bytes)
        for ver_el in root.findall(".//Vereador"):
            id_parl = ver_el.attrib.get("IDParlamentar", "")
            if not id_parl.isdigit():
                continue
            vid = int(id_parl)
            idx = vid_to_idx.get(vid)
            if idx is None:
                continue
            r = ver_records[idx]
            for sess in ver_el.findall("Sessao"):
                d_iso = parse_date_iso(sess.attrib.get("Dia", ""))
                if d_iso:
                    sess_dates.add(d_iso)
                p_txt = (sess.attrib.get("Presenca") or "").strip()
                if p_txt == "*":
                    continue
                elif p_txt.lower().startswith("presente"):
                    r["P"] += 1
                elif p_txt == "":
                    r["F"] += 1
                else:
                    r["A"] += 1

    sess_list = [{"d": d} for d in sorted(sess_dates)]
    ult_sessao = sess_list[-1]["d"] if sess_list else "2026-11-12"

    # 5. Proposições 2025 e 2026 + Autores + Deliberações
    print("  -> Processando proposições 2025 e 2026 (SPLegis)...")
    proj_2025 = fetch_cached_or_url(
        "proj_2025_json.dat",
        "https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/ProjetosPorAnoJSON?Ano=2025",
        is_json=True,
    )
    proj_2026 = fetch_cached_or_url(
        "proj_2026_json.json",
        "https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/ProjetosPorAnoJSON?Ano=2026",
        is_json=True,
    )

    status_by_key = {}
    for ano, fn_d, fn_e in [
        (2025, "delib_2025_json.json", "encerr_2025_json.json"),
        (2026, "delib_2026_json.dat", "encerr_2026_json.dat"),
    ]:
        try:
            delibs = fetch_cached_or_url(
                fn_d,
                f"https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/ProjetosDeliberacaoJSON?Ano={ano}",
                is_json=True,
            )
            for d in delibs if isinstance(delibs, list) else []:
                tp_d = str(d.get("tipo") or d.get("sigla") or "").upper().strip()
                k = (tp_d, int(d.get("numero", 0)), int(d.get("ano", ano)))
                status_by_key[k] = ("Aprovado", "a")
        except Exception:
            pass
        try:
            encerrs = fetch_cached_or_url(
                fn_e,
                f"https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/ProjetosEncerradosJSON?Ano={ano}",
                is_json=True,
            )
            for e in encerrs if isinstance(encerrs, list) else []:
                tp_e = str(e.get("tipo") or e.get("sigla") or "").upper().strip()
                k = (tp_e, int(e.get("numero", 0)), int(e.get("ano", ano)))
                cond = (e.get("motivo") or e.get("condicao") or "").strip()
                if "LEI" in cond.upper() or "APROV" in cond.upper() or "PROMULG" in cond.upper():
                    status_by_key[k] = ("Aprovado / Lei", "a")
                elif "REJEIT" in cond.upper() or "VETAD" in cond.upper() or "RETIR" in cond.upper():
                    status_by_key[k] = (cond.title(), "r")
                else:
                    status_by_key[k] = (cond.title() if cond else "Concluído", "a")
        except Exception:
            pass

    tipos_prop = ["PL", "PLO", "PDL", "PR", "IND", "MOC", "RDS", "RPS", "RDP", "RPP", "RPL"]
    autores_by_key = defaultdict(list)

    def _fetch_autores_tipo(args):
        ano, tp = args
        cfile = f"aut_{ano}_{tp}.json"
        url = f"https://splegisws.saopaulo.sp.leg.br/ws/ws2.asmx/ProjetosAutoresJSON?ano={ano}&tipo={tp}&numero=0"
        try:
            arr = fetch_cached_or_url(cfile, url, is_json=True)
            return ano, tp, arr if isinstance(arr, list) else []
        except Exception:
            return ano, tp, []

    tasks = [(ano, tp) for ano in (2025, 2026) for tp in tipos_prop]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for ano, tp, arr in ex.map(_fetch_autores_tipo, tasks):
            for rec in arr:
                tp_r = str(rec.get("tipo") or rec.get("sigla") or tp).upper().strip()
                k = (tp_r, int(rec.get("numero", 0)), int(rec.get("ano", ano)))
                for a in rec.get("autores", []):
                    autores_by_key[k].append((int(a.get("chave", 0)), a.get("nome", ""), "S"))
                for s in rec.get("subscritores", []):
                    autores_by_key[k].append((int(s.get("chave", 0)), s.get("nome", ""), "S"))
                for c in rec.get("coautores", []):
                    autores_by_key[k].append((int(c.get("chave", 0)), c.get("nome", ""), "C"))

    TYPE_NAMES_CMSP = {
        "PL": "Projeto de Lei Ordinária",
        "PLO": "Proposta de Emenda à Lei Orgânica",
        "PDL": "Projeto de Decreto Legislativo",
        "PR": "Projeto de Resolução",
        "IND": "Indicação",
        "MOC": "Moção",
        "RDS": "Requerimento",
        "RPS": "Requerimento",
        "RDP": "Requerimento de Informação",
        "RPP": "Requerimento",
        "RPL": "Requerimento",
    }

    items = []
    catot = {"b": 0, "c": 0, "h": 0, "l": 0, "o": 0}
    v_cats = [{"b": 0, "c": 0, "h": 0, "l": 0, "o": 0} for _ in ver_records]
    v_cob_pend = [0 for _ in ver_records]
    coaut_counts = [Counter() for _ in ver_records]
    cob_tot_all = 0
    cob_pend_all = 0
    cob_resp_all = 0

    pref_items = []
    pref_plo = 0
    pref_plc = 0
    pref_aprov = 0
    pref_tram = 0
    pref_rej = 0

    all_projs_raw = []
    for ano, plist in [(2025, proj_2025), (2026, proj_2026)]:
        for p in plist if isinstance(plist, list) else []:
            all_projs_raw.append((ano, p))

    # Ordenar da mais recente para a mais antiga
    all_projs_raw.sort(
        key=lambda ap: (parse_date_iso(ap[1].get("data", "")).replace("-", ""), int(ap[1].get("numero", 0))),
        reverse=True,
    )

    for ano, p in all_projs_raw:
        sigla = str(p.get("tipo") or p.get("sigla") or "").upper().strip()
        numero = int(p.get("numero", 0))
        dt_iso = parse_date_iso(p.get("data", f"01/01/{ano}"))
        aaaammdd = dt_iso.replace("-", "")
        ementa = re.sub(r"\s+", " ", (p.get("ementa") or "").strip())
        if not ementa:
            continue
        k = (sigla, numero, ano)
        aut_list = autores_by_key.get(k, [])
        pid = str(p.get("chave", f"{sigla}-{numero}-{ano}"))
        code = f"{sigla} {numero}/{ano}"
        tp_full = TYPE_NAMES_CMSP.get(sigla, sigla)

        res_label, st_code = status_by_key.get(k, ("Em tramitação", "t"))
        if sigla in ("IND", "RDS", "RPS") and st_code == "t":
            res_label = "Encaminhado ao Executivo"
            st_code = "p"

        # Projeto de autoria do Prefeito Ricardo Nunes (chave 2229)
        is_exec = any(aid == 2229 or "RICARDO NUNES" in nm.upper() for aid, nm, _ in aut_list)
        if is_exec and sigla in ("PL", "PLO", "PDL", "PR"):
            if sigla == "PL":
                pref_plo += 1
            elif sigla == "PLO":
                pref_plc += 1
            if st_code == "a":
                pref_aprov += 1
            elif st_code == "r":
                pref_rej += 1
            else:
                pref_tram += 1
            pref_items.append([pid, code, ementa, aaaammdd, sigla, res_label, st_code])

        # Vereadores titulares autores
        ver_idxs = sorted({vid_to_idx[aid] for aid, _, _ in aut_list if aid in vid_to_idx})
        if not ver_idxs:
            continue

        grupo, eh_materia, th = classify_item(tp_full, ementa)
        if grupo == "c":
            cob_tot_all += 1
            if st_code in ("p", "t"):
                cob_pend_all += 1
                st_code = "p"
                for idx_v in ver_idxs:
                    v_cob_pend[idx_v] += 1
            elif st_code == "a":
                cob_resp_all += 1

        if len(ver_idxs) > 1:
            for i_a in ver_idxs:
                for j_a in ver_idxs:
                    if i_a != j_a:
                        coaut_counts[i_a][j_a] += 1

        items.append([
            pid,
            code,
            ementa,
            aaaammdd,
            grupo,
            res_label,
            ver_idxs,
            eh_materia,
            th,
            st_code,
        ])
        catot[grupo] += 1
        for idx_v in ver_idxs:
            v_cats[idx_v][grupo] += 1

    # 6. Votações Nominais em Plenário (VOTACOES_2025.xml e VOTACOES_2026.xml)
    print("  -> Processando votações nominais (2025 e 2026)...")
    nvot = 0
    unan = 0
    n_ver = len(ver_records)
    v_votes = [{"vs": 0, "vn": 0, "va": 0, "naos": [], "alin_s": 0, "alin_t": 0} for _ in ver_records]
    vmat = [[[0, 0] for _ in range(n_ver)] for _ in range(n_ver)]
    rachas_list = []
    vetos_by_title = {}

    for ano in (2025, 2026):
        xml_bytes = fetch_cached_or_url(
            f"vot_{ano}_xml.dat",
            f"https://splegispdarmazenamento.blob.core.windows.net/containersip/VOTACOES_{ano}.xml",
            is_json=False,
        )
        root = ET.fromstring(xml_bytes)
        for sess_el in root.findall(".//Sessao"):
            dt_sess = parse_date_iso(sess_el.attrib.get("Data", f"01/01/{ano}"))
            for vot_el in sess_el.findall("Votacao"):
                tp_vot = (vot_el.attrib.get("TipoVotacao") or "").strip()
                if tp_vot.lower() != "nominal":
                    continue
                materia = re.sub(r"\s+", " ", (vot_el.attrib.get("Materia") or "").strip())
                ementa_v = re.sub(r"\s+", " ", (vot_el.attrib.get("Ementa") or "").strip())
                res_v = (vot_el.attrib.get("Resultado") or "").strip()
                notas_v = re.sub(r"\s+", " ", (vot_el.attrib.get("NotasRodape") or "").strip())

                sim_indices = []
                nao_indices = []
                abst_indices = []
                ver_vote_map = {}

                for ver_v in vot_el.findall("Vereador"):
                    id_p = ver_v.attrib.get("IDParlamentar", "")
                    if not id_p.isdigit():
                        continue
                    idx = vid_to_idx.get(int(id_p))
                    if idx is None:
                        continue
                    voto_low = norm(ver_v.attrib.get("Voto") or "")
                    if voto_low.startswith("sim") or voto_low.startswith("favor"):
                        ver_vote_map[idx] = "Sim"
                        sim_indices.append(idx)
                    elif voto_low.startswith("nao") or voto_low.startswith("contra"):
                        ver_vote_map[idx] = "Não"
                        nao_indices.append(idx)
                    elif voto_low.startswith("abst"):
                        ver_vote_map[idx] = "Abstenção"
                        abst_indices.append(idx)

                if not sim_indices and not nao_indices:
                    continue

                nvot += 1
                is_unan = len(nao_indices) == 0 and len(abst_indices) == 0
                if is_unan:
                    unan += 1

                full_txt_v = f"{materia} {ementa_v} {notas_v}".upper()
                is_veto = "VETO" in materia.upper()
                is_pref_matter = is_veto or any(w in full_txt_v for w in ("EXECUTIVO", "PREFEITO", "RICARDO NUNES"))
                aut_label = "Poder Executivo (Ricardo Nunes)" if is_pref_matter else "Legislativo Municipal"
                desc_v = (ementa_v or notas_v or materia)[:280]

                for idx, v_str in ver_vote_map.items():
                    if is_pref_matter:
                        v_votes[idx]["alin_t"] += 1
                        if (not is_veto and v_str == "Sim") or (is_veto and v_str == "Sim"):
                            v_votes[idx]["alin_s"] += 1
                    if v_str == "Sim":
                        v_votes[idx]["vs"] += 1
                    elif v_str == "Não":
                        v_votes[idx]["vn"] += 1
                        v_votes[idx]["naos"].append({
                            "t": materia[:60],
                            "d": dt_sess,
                            "e": desc_v,
                            "u": "https://www.saopaulo.sp.leg.br/atividade-legislativa/votacoes-no-plenario/",
                            "v": "nao",
                            "a": aut_label,
                        })
                    elif v_str == "Abstenção":
                        v_votes[idx]["va"] += 1
                        v_votes[idx]["naos"].append({
                            "t": materia[:60],
                            "d": dt_sess,
                            "e": desc_v,
                            "u": "https://www.saopaulo.sp.leg.br/atividade-legislativa/votacoes-no-plenario/",
                            "v": "abst",
                            "a": aut_label,
                        })

                sim_indices.sort()
                nao_indices.sort()
                abst_indices.sort()

                voted_idxs = list(ver_vote_map.keys())
                for i1 in range(len(voted_idxs)):
                    u1 = voted_idxs[i1]
                    v1 = ver_vote_map[u1]
                    for i2 in range(i1, len(voted_idxs)):
                        u2 = voted_idxs[i2]
                        v2 = ver_vote_map[u2]
                        vmat[u1][u2][1] += 1
                        if u1 != u2:
                            vmat[u2][u1][1] += 1
                        if v1 == v2:
                            vmat[u1][u2][0] += 1
                            if u1 != u2:
                                vmat[u2][u1][0] += 1

                if is_veto:
                    res_up = res_v.upper()
                    v_status = "Derrubado" if ("REJEITAD" in res_up or "DERRUBAD" in res_up) else "Mantido"
                    vetos_by_title[f"{materia}_{dt_sess}"] = {
                        "t": materia[:60],
                        "d": dt_sess,
                        "e": desc_v,
                        "aut": aut_label,
                        "status": v_status,
                        "s": len(sim_indices),
                        "n": len(nao_indices),
                        "a": len(abst_indices),
                        "u": "https://www.saopaulo.sp.leg.br/atividade-legislativa/votacoes-no-plenario/",
                        "vs": sim_indices,
                        "vn": nao_indices,
                        "va": abst_indices,
                    }

                if not is_unan:
                    res_up = res_v.upper()
                    res_clean = "Aprovado" if "APROVAD" in res_up else ("Rejeitado" if "REJEITAD" in res_up else (res_v or "Votado"))
                    rachas_list.append({
                        "t": materia[:60],
                        "d": dt_sess,
                        "turno": "Nominal",
                        "e": desc_v,
                        "aut": aut_label,
                        "res": res_clean,
                        "u": "https://www.saopaulo.sp.leg.br/atividade-legislativa/votacoes-no-plenario/",
                        "s": sim_indices,
                        "n": nao_indices,
                        "a": abst_indices,
                        "pref": 1 if is_pref_matter else 0,
                    })

    rachas_list.sort(
        key=lambda x: (min(len(x["s"]), len(x["n"]) + len(x["a"])), len(x["n"]) + len(x["a"]), x["d"]),
        reverse=True,
    )
    vetos_list = sorted(vetos_by_title.values(), key=lambda x: x["d"], reverse=True)
    vetos_mant = sum(1 for x in vetos_list if x["status"] == "Mantido")
    vetos_derr = sum(1 for x in vetos_list if x["status"] == "Derrubado")

    v_out = []
    for idx, r in enumerate(ver_records):
        tse_m = r["tse"]
        alin_s = v_votes[idx]["alin_s"]
        alin_t = v_votes[idx]["alin_t"]
        alin_pct = round(100 * alin_s / alin_t) if alin_t > 0 else 0
        top_parc = [[other_i, cnt] for other_i, cnt in coaut_counts[idx].most_common(3)]
        naos_sorted = sorted(v_votes[idx]["naos"], key=lambda x: x["d"], reverse=True)

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
            "vs": v_votes[idx]["vs"],
            "vn": v_votes[idx]["vn"],
            "va": v_votes[idx]["va"],
            "naos": naos_sorted,
            "cat": v_cats[idx],
            "url": r["url"],
            "foto": r["foto"],
            "foto_url": r["foto_url"],
            "mand": r["mand"],
            "desde": r["desde"],
            "mand_txt": r["mand_txt"],
            "mand_hist": r["mand_hist"],
            "nota_exec": "",
            "leis_tot": r["leis_tot"],
            "leis_ord": r["leis_ord"],
            "leis_det": {"Normas sancionadas": r["leis_tot"]},
            "coms": r["coms"],
            "em_imp": [],
            "alin": alin_pct,
            "alin_s": alin_s,
            "alin_t": alin_t,
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
            "plc": pref_plc,
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
        "rachas": rachas_list,
        "vmat": vmat,
    }
