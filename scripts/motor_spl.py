#!/usr/bin/env python3
"""
Motor Coletor SPL (Câmara Sem Papel) — São José dos Campos (21), Taubaté (19) e Caçapava (10).
Implementa todas as regras de blindagem técnica (Seção 4 do escopo):
1. Corte estrito antes de Suplentes/Licenciados/Inativos e validação de cadeiras constitucionais;
2. Extração do novo layout de parlamentar.aspx (nome_parlamentar, partido entre parênteses, aid de consulta-producao, tab_leis_autoria, tab_frequencia_plenario);
3. Cruzamento com TSE (2012–2026) desambiguado por CPF/Título/NM_UE;
4. ThreadPoolExecutor(max_workers=6), retry com backoff e SSL resiliente;
5. Suporte aos dois formatos de PDF de votação nominal do SPL ("Relatório de Votação" de Taubaté e "Extrato de Votação Eletrônica" de SJC).
"""

import concurrent.futures
import csv
import datetime
import glob
import html
import http.cookiejar
import io
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
import zlib
from collections import Counter

from common_rx import (
    classify_item,
    clean_ap,
    compute_bairros_and_rep,
    load_tse_city,
    match_tse_vereador,
    norm,
)

SSL_CTX = ssl._create_unverified_context()

TIPO_ABBR = {
    "Requerimento": "REQ",
    "Indicação": "IND",
    "Moção": "MOC",
    "Projeto de Lei Ordinária": "PL",
    "Projeto de Lei": "PL",
    "Projeto de Lei Complementar": "PLC",
    "Projeto de Decreto Legislativo": "PDL",
    "Projeto de Resolução": "PRE",
    "Proposta de Emenda à Lei Orgânica do Município": "PEL",
    "Emenda à Lei Orgânica": "PEL",
    "Emenda": "Emenda",
    "Substitutivo": "Substitutivo",
    "Frente Parlamentar": "FRP",
    "Nome Parlamentar": "NPA",
    "Ofício de Veto": "VETO",
}


def http_get(url: str, timeout: int = 30) -> bytes:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (RadarCidadao/1.0)"})
            return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX).read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return b""


def parse_csv_content(raw_text: str):
    lines = raw_text.splitlines()
    joined = []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("ID,Processo,Ano,") or re.match(r"^\d+,\d+,\d{4},", line):
            joined.append(line)
        elif joined:
            joined[-1] = joined[-1] + " " + line.strip()
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO("\n".join(joined)))):
        if i == 0 or len(r) < 14:
            continue
        if not re.match(r"^\d{2}/\d{2}/\d{4}$", r[-5].strip()):
            continue
        rows.append({
            "id": r[0].strip(),
            "proc": r[1].strip(),
            "ano_proc": r[2].strip(),
            "tipo": r[3].strip(),
            "num": r[4].strip(),
            "ano": r[5].strip(),
            "ementa": ", ".join(r[6:-7]).strip().strip('"').strip(),
            "protocolo": r[-7].strip(),
            "autor": r[-6].strip(),
            "data": r[-5].strip(),
            "fase": r[-4].strip(),
            "setor": r[-3].strip(),
            "arq_setor": r[-2].strip(),
            "situacao": r[-1].strip(),
        })
    return rows


def fetch_csv_from_spl(url: str) -> str:
    for attempt in range(3):
        try:
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cj),
                urllib.request.HTTPSHandler(context=SSL_CTX),
            )
            opener.addheaders = [("User-Agent", "Mozilla/5.0 (RadarCidadao/1.0)")]
            page_html = opener.open(url, timeout=30).read().decode("utf-8", errors="ignore")
            m_vs = re.search(r'id="__VIEWSTATE" value="([^"]*)"', page_html)
            if not m_vs:
                return ""
            vs = m_vs.group(1)
            vsg = re.search(r'id="__VIEWSTATEGENERATOR" value="([^"]*)"', page_html).group(1)
            ev_m = re.search(r'id="__EVENTVALIDATION" value="([^"]*)"', page_html)
            post_dict = {
                "__VIEWSTATE": vs,
                "__VIEWSTATEGENERATOR": vsg,
                "ctl00$ContentPlaceHolder1$btn_export_csv": "CSV",
            }
            if ev_m:
                post_dict["__EVENTVALIDATION"] = ev_m.group(1)
            data = urllib.parse.urlencode(post_dict).encode("utf-8")
            resp = opener.open(url, data=data, timeout=60)
            ctype = resp.headers.get("Content-Type", "")
            if "CSV" not in ctype.upper():
                return ""
            raw_b = resp.read()
            try:
                return raw_b.decode("utf-8-sig")
            except UnicodeDecodeError:
                return raw_b.decode("latin1", errors="replace")
        except Exception:
            if attempt == 2:
                return ""
            time.sleep(2 * (attempt + 1))
    return ""


def extract_pdf_tokens(dec: str):
    tokens = []
    for m in re.finditer(r"\[(.*?)\]\s*TJ|\((.*?)\)\s*Tj", dec, re.S):
        if m.group(1) is not None:
            parts = re.findall(r"\((.*?)\)", m.group(1))
            s = "".join(parts)
        else:
            s = m.group(2)
        s = s.replace(r"\(", "(").replace(r"\)", ")").strip()
        if s:
            tokens.append(s)
    return tokens


def parse_pdf_votes(raw_pdf: bytes, sid: str, dt_iso: str, purl: str):
    votes = []
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw_pdf, re.S)
    for st in streams:
        dec = None
        for wb in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                dec = zlib.decompress(st, wb).decode("latin1")
                break
            except Exception:
                pass
        if not dec:
            continue
        tokens = extract_pdf_tokens(dec)
        if not tokens:
            continue

        # Formato 1: Taubaté ("Relatório de Votação" / "Lista de votação")
        if any(k in tokens[0] for k in ("Relatório de Votação", "Lista de votação")) and "Parlamentar" in tokens and "Totais:" in tokens:
            sess_name = tokens[2] if len(tokens) > 2 else ""
            prop_title = tokens[3] if len(tokens) > 3 else ""
            turno = tokens[4] if len(tokens) > 4 else ""
            idx_nom = tokens.index("Nominal") if "Nominal" in tokens else 11
            idx_parl = tokens.index("Parlamentar")
            idx_hora = tokens.index("Hora")
            idx_tot = tokens.index("Totais:")

            meta_tokens = tokens[idx_nom + 1 : idx_parl]
            autoria = ""
            ementa_parts = []
            for mt in meta_tokens:
                if mt.startswith("Autoria:"):
                    autoria = mt.replace("Autoria:", "").strip()
                else:
                    ementa_parts.append(mt)
            ementa = " ".join(ementa_parts).strip()

            votos_parl = {}
            i = idx_hora + 1
            while i < idx_tot:
                tk = tokens[i]
                m_p = re.match(r"^([A-ZÀ-Ú0-9\s\.\-]+?)\s*\(([^)]+)\)$", tk)
                if m_p and i + 1 < idx_tot:
                    pname = m_p.group(1).strip()
                    voto = tokens[i + 1].strip()
                    votos_parl[pname] = voto
                    i += 2
                    if i < idx_tot and re.match(r"^\d{2}:\d{2}:\d{2}$", tokens[i]):
                        i += 1
                else:
                    i += 1

            resultado = ""
            if "Resultado:" in tokens:
                ir = tokens.index("Resultado:")
                if ir > 0:
                    resultado = tokens[ir - 1]

            votes.append({
                "sid": sid,
                "data": dt_iso,
                "sess_name": sess_name,
                "prop_title": prop_title,
                "turno": turno,
                "autoria": autoria,
                "ementa": ementa,
                "resultado": resultado,
                "url": purl,
                "votos": votos_parl,
            })
            continue

        # Formato 2: São José dos Campos ("EXTRATO DE VOTAÇÃO ELETRÔNICA")
        if "EXTRATO DE VOTAÇÃO" in " ".join(tokens[:4]).upper():
            # Encontrar blocos iniciados por "N\" ou "Processo nº" antes de "Resultado:"
            res_indices = [idx for idx, t in enumerate(tokens) if t == "Resultado:"]
            prev_end = 0
            for r_idx in res_indices:
                header_slice = tokens[prev_end:r_idx]
                # Achar início do item (ex: '1\', '2\' ou 'Processo nº')
                start_local = 0
                for j in range(len(header_slice) - 1, -1, -1):
                    if re.match(r"^\d+\\$", header_slice[j]):
                        start_local = j + 1
                        break
                item_hdr = header_slice[start_local:]
                prop_title = ""
                autoria = ""
                ementa_parts = []
                for tk in item_hdr:
                    if tk == "-":
                        continue
                    if re.search(r"(?:Projeto de Lei|Projeto de Decreto|Projeto de Resolu|Requerimento|Moç|Veto|Emenda)\s*n[º°]", tk, re.I) and not prop_title:
                        prop_title = tk.strip()
                    elif tk.startswith("Autoria:"):
                        autoria = re.sub(r"^Autoria:\s*(?:Ver\.ª|Ver\.|Vereador(?:a)?)?\s*", "", tk, flags=re.I).strip()
                    elif not tk.startswith("Processo nº") and not tk.startswith("Tramitação d"):
                        ementa_parts.append(tk)
                    elif tk.startswith("Tramitação d") and not prop_title:
                        prop_title = tk[:70]
                if not prop_title and item_hdr:
                    prop_title = item_hdr[0]
                ementa = " ".join(ementa_parts).strip()

                # Resultado fica logo após 'Resultado:' até 'Quórum'
                q_idx = r_idx + 1
                while q_idx < len(tokens) and tokens[q_idx] != "Quórum":
                    q_idx += 1
                resultado = "".join(tokens[r_idx + 1 : q_idx]).strip()

                # Pular cabeçalho da tabela: Quórum, Presentes, Favoráveis, Contrários, Abstenções + 5 valores
                v_start = q_idx + 10
                # Fim do bloco de votos: próximo 'N\' ou rodapé '* O presidente'
                v_end = len(tokens)
                for k in range(v_start, len(tokens)):
                    if re.match(r"^\d+\\$", tokens[k]) or tokens[k].startswith("* O presidente"):
                        v_end = k
                        break
                prev_end = v_end

                votos_parl = {}
                k = v_start
                while k + 1 < v_end:
                    pname = tokens[k].strip()
                    vraw = tokens[k + 1].strip()
                    if vraw in ("Favorável", "Contrário", "Abstenção", "Presidente*", "Ausente"):
                        if vraw == "Favorável":
                            votos_parl[pname] = "Sim"
                        elif vraw == "Contrário":
                            votos_parl[pname] = "Não"
                        elif vraw == "Abstenção":
                            votos_parl[pname] = "Abstenção"
                        k += 2
                    else:
                        k += 1

                if votos_parl:
                    votes.append({
                        "sid": sid,
                        "data": dt_iso,
                        "sess_name": "Sessão Ordinária",
                        "prop_title": prop_title,
                        "turno": "Votação Nominal",
                        "autoria": autoria,
                        "ementa": ementa or prop_title,
                        "resultado": resultado,
                        "url": purl,
                        "votos": votos_parl,
                    })
    return votes


def collect_spl_city(cfg: dict, city_dir: str, scratch_dir: str):
    spl_base = cfg["spl_base"].rstrip("/")
    spl_root = spl_base[:-4] if spl_base.endswith("/spl") else spl_base
    os.makedirs(scratch_dir, exist_ok=True)
    os.makedirs(os.path.join(scratch_dir, "csv"), exist_ok=True)
    os.makedirs(os.path.join(city_dir, "fotos"), exist_ok=True)

    # 1. Mapear autores em consulta-autor.aspx
    h_autor = http_get(f"{spl_base}/consulta-autor.aspx").decode("utf-8", errors="ignore")
    autor_map = {}
    for m in re.finditer(r"card-title[^>]*>(.*?)</div>.*?consulta-producao\.aspx\?autor=(\d+)", h_autor, re.S):
        name = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        name = re.sub(r"\(\d+\)$", "", name).strip()
        autor_map[norm(name)] = m.group(2)

    # 2. Listar vereadores titulares em parlamentares.aspx (corte estrito antes de Suplentes/Licenciados/Inativos)
    h_parl = http_get(f"{spl_base}/parlamentares.aspx").decode("utf-8", errors="ignore")
    if 'id="ContentPlaceHolder1_parlamentares_lista"' in h_parl:
        h_parl = h_parl.split('id="ContentPlaceHolder1_parlamentares_lista"', 1)[1]
    if 'id="div_footer_parlamentares"' in h_parl:
        h_parl = h_parl.split('id="div_footer_parlamentares"', 1)[0]
    main_part = re.split(
        r"(?:Suplentes\s+em\s+Exerc[ií]cio|Ex-suplentes|Suplentes|Licenciados|Inativos)",
        h_parl,
        maxsplit=1,
        flags=re.I,
    )[0]
    pids = []
    for m in re.finditer(r"parlamentar\.aspx\?id=(\d+)", main_part):
        if m.group(1) not in pids:
            pids.append(m.group(1))

    # Consultar mesas.aspx para mapear cargos da Mesa Diretora caso não conste no perfil individual
    mesa_by_pid = {}
    try:
        h_mesa = http_get(f"{spl_base}/mesas.aspx").decode("utf-8", errors="ignore")
        for c in re.split(r"<div class=['\"]kt-portlet ", h_mesa)[1:]:
            pm = re.search(r"parlamentar\.aspx\?id=(\d+)", c)
            cm = re.search(r"(Presidente|1º Vice-Presidente|2º Vice-Presidente|Vice-Presidente|1º Secretário|2º Secretário|1ª Secretária|2ª Secretária)", c, re.I)
            if pm and cm and pm.group(1) not in mesa_by_pid:
                mesa_by_pid[pm.group(1)] = cm.group(1)
    except Exception:
        pass

    cur_year = datetime.date.today().year
    years = [str(y) for y in range(2025, cur_year + 1)]

    def fetch_one_vereador(pid: str):
        url = f"{spl_base}/parlamentar.aspx?id={pid}"
        phtml = http_get(url).decode("utf-8", errors="ignore")
        ap_m = (
            re.search(r'id="nome_parlamentar"[^>]*>(.*?)</div>', phtml, re.S)
            or re.search(r"<a href='#' class='kt-widget__title'>(.*?)</a>", phtml, re.S)
        )
        ap_raw = html.unescape(re.sub(r"<[^>]+>", "", ap_m.group(1)).strip()) if ap_m else ""
        ap = clean_ap(ap_raw)

        part_m = (
            re.search(r'id="partido"[^>]*>(.*?)</span>', phtml, re.S)
            or re.search(r"<b>Partido:\s*</b>([^<]+)", phtml)
        )
        part_raw = html.unescape(re.sub(r"<[^>]+>", "", part_m.group(1)).strip()) if part_m else ""
        part_paren = re.search(r"\(([^)]+)\)", part_raw)
        part = part_paren.group(1).strip() if part_paren else part_raw
        if part.upper() == "PROGRESSISTAS":
            part = "PP"
        elif part.upper() == "UNIÃO BRASIL":
            part = "UNIÃO"
        elif part.upper() == "PODEMOS":
            part = "PODE"

        # Baixar foto oficial
        foto_ok = 0
        foto_path = os.path.join(city_dir, "fotos", f"{pid}.jpg")
        img_m = re.search(r"(https?://[^\"'\s>]+/arquivo/images/pessoas/[^\"'\s>]+)", phtml, re.I)
        if img_m:
            try:
                if not os.path.exists(foto_path) or os.path.getsize(foto_path) < 500:
                    fb = http_get(img_m.group(1), timeout=20)
                    if len(fb) > 500:
                        with open(foto_path, "wb") as out_f:
                            out_f.write(fb)
                if os.path.exists(foto_path) and os.path.getsize(foto_path) > 500:
                    foto_ok = 1
            except Exception:
                pass
        elif os.path.exists(foto_path) and os.path.getsize(foto_path) > 500:
            foto_ok = 1

        leg_curr_block = re.split(r"\b(?:18|32)ª Legislatura\b", phtml)[0]
        mesa_m = re.search(r"Participação na Mesa Diretora.*?<strong[^>]*>(.*?)</strong>", leg_curr_block, re.S)
        if mesa_m:
            mesa_raw = html.unescape(re.sub(r"<[^>]+>", " ", mesa_m.group(1)).strip())
        else:
            mesa_raw = mesa_by_pid.get(pid, "")

        def get_info(label):
            m = re.search(rf"kt-widget__label'>\s*{label}:\s*</span>\s*<span[^>]*>(.*?)</span>", phtml, re.S)
            return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""

        nome_civil = get_info("Nome civil")
        tel = get_info(r"Telefone\(s\)")
        email = get_info("E-mail")

        bio_m = (
            re.search(r'id="texto_biografia"[^>]*>(.*?)<div class="kt-timeline', phtml, re.S)
            or re.search(r"Biografia Parlamentar\s*</h3>.*?<div class=\"kt-widget12__info\">(.*?)</div>", phtml, re.S)
        )
        bio = html.unescape(re.sub(r"<[^>]+>", " ", bio_m.group(1)).strip()) if bio_m else ""

        legislaturas = []
        seen_legs = set()
        for rm in re.finditer(r"title=['\"](\d+ª Legislatura)\s*\((\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})\)['\"]", phtml):
            leg, ini_l, fim_l = rm.group(1).strip(), rm.group(2).strip(), rm.group(3).strip()
            if leg not in seen_legs:
                seen_legs.add(leg)
                legislaturas.append({"leg": leg, "ini": ini_l, "fim": fim_l})

        comissoes = []
        for cm in re.finditer(r"Cargo:\s*([^\(<]+)\(([^)]+)\)\s*</h3>\s*<span[^>]*>.*?<a[^>]*>(.*?)</a>", leg_curr_block, re.S):
            c_cargo = html.unescape(cm.group(1).strip())
            c_per = html.unescape(cm.group(2).strip())
            c_nome = html.unescape(re.sub(r"<[^>]+>", "", cm.group(3)).strip())
            c_nome = re.sub(r"\s*-\s*\d+ª\s*Formação.*$", "", c_nome, flags=re.I).strip()
            if "2026" in c_per or "2025" in c_per:
                comissoes.append({"cargo": c_cargo, "nome": c_nome})

        emendas_imp = []
        for em in re.finditer(r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>\s*(202\d)\s*</a>", phtml, re.I):
            u_em = em.group(1)
            if not u_em.startswith("http"):
                u_em = spl_root + "/" + u_em.lstrip("/")
            emendas_imp.append({"ano": em.group(2), "url": u_em})

        leis_count = {}
        if 'id="tab_leis_autoria"' in phtml:
            leis_tab = phtml.split('id="tab_leis_autoria"')[1].split('id="tab_frequencia_plenario"')[0]
            for lm in re.finditer(r"(Lei Ordinária|Lei Complementar|Decreto Legislativo|Emenda à Lei Orgânica|Resolução|Lei)\s*<span[^>]*>(\d+)</span>", leis_tab, re.S):
                k_l = "Lei Ordinária" if lm.group(1) == "Lei" else lm.group(1)
                leis_count[k_l] = leis_count.get(k_l, 0) + int(lm.group(2))

        freq = {}
        if 'id="tab_frequencia_plenario"' in phtml:
            f_sub = phtml.split('id="tab_frequencia_plenario"')[1]
            for fm in re.finditer(r"<h3 class=['\"]kt-widget1__title['\"]>(.*?)</h3>(.*?)(?=<div class=['\"]kt-widget1__item['\"]|</div>\s*</div>\s*</div>)", f_sub, re.S):
                status_name = html.unescape(re.sub(r"<[^>]+>", "", fm.group(1)).strip())
                yr_pairs = dict(re.findall(r"<big[^>]*>(\d{4})</big>\s*<span[^>]*>(\d+)</span>", fm.group(2)))
                freq[status_name] = {
                    "2026": int(yr_pairs.get("2026", 0)),
                    "2025": int(yr_pairs.get("2025", 0)),
                }

        aid_m = re.search(r"consulta-producao\.aspx\?tipo=\d+&(?:amp;)?autor=(\d+)", phtml)
        aid = aid_m.group(1) if aid_m else autor_map.get(norm(ap), pid)
        tipos_links = sorted(set(re.findall(r"consulta-producao\.aspx\?tipo=(\d+)&(?:amp;)?autor=\d+", phtml)))

        for ano in years:
            csv_file = os.path.join(scratch_dir, "csv", f"autor_{aid}_{ano}.csv")
            if os.path.exists(csv_file) and os.path.getsize(csv_file) > 100:
                continue
            u_csv = f"{spl_base}/consulta-producao.aspx?autor={aid}&ano={ano}"
            raw_csv = fetch_csv_from_spl(u_csv)
            if raw_csv:
                with open(csv_file, "w", encoding="utf-8") as out:
                    out.write(raw_csv)
            else:
                combined = []
                for t_id in (tipos_links or ["1", "2", "3", "4", "5", "6", "8", "9", "10", "11", "316", "319", "340", "341", "344", "345", "346", "347", "348"]):
                    sub_csv = fetch_csv_from_spl(f"{spl_base}/consulta-producao.aspx?tipo={t_id}&autor={aid}&ano={ano}")
                    if sub_csv:
                        lines = sub_csv.splitlines()
                        if not combined and lines:
                            combined.append(lines[0])
                        combined.extend(lines[1:])
                if combined:
                    with open(csv_file, "w", encoding="utf-8") as out:
                        out.write("\n".join(combined))

        return {
            "pid": pid,
            "aid": aid,
            "ap": ap,
            "nome_civil": nome_civil or ap,
            "part": part,
            "tel": tel,
            "email": email,
            "url": url,
            "foto": foto_ok,
            "mesa_raw": mesa_raw,
            "freq": freq,
            "bio": bio,
            "legislaturas": legislaturas,
            "comissoes": comissoes,
            "leis": leis_count,
            "emendas_impositivas": emendas_imp,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        vereadores = list(ex.map(fetch_one_vereador, pids))

    # Caso especial Caçapava: Câmara tem 10 cadeiras constitucionais (11 listados no bloco Ativo por suplência).
    # Mantemos os 10 titulares com maior presença e votação titular em 2024:
    if cfg["slug"] == "cacapava" and len(vereadores) > cfg["cadeiras"]:
        tse_city = load_tse_city("cacapava")
        for v in vereadores:
            tm = match_tse_vereador(v["ap"], v["nome_civil"], tse_city["vereadores"])
            v["_votos24"] = tm.get("votos24", 0)
        # Os 10 vereadores eleitos por QP/Média em 2024 com mais votos (excluindo a 11ª vaga extra/suplente)
        vereadores.sort(key=lambda x: -x["_votos24"])
        vereadores = vereadores[: cfg["cadeiras"]]

    if len(vereadores) != cfg["cadeiras"]:
        raise RuntimeError(
            f"[{cfg['slug']}] Validação de cadeiras falhou: esperado {cfg['cadeiras']}, obtido {len(vereadores)}"
        )

    # 2b. Coletar proposições do Prefeito / Poder Executivo
    exec_aids = []
    for k_aut, v_aid in autor_map.items():
        if any(w in k_aut for w in ("prefeito municipal", "poder executivo", "chefe do executivo")):
            exec_aids.append(v_aid)
    if not exec_aids:
        exec_aids = ["20"]
    for pref_aid in exec_aids[:2]:
        for ano in years:
            p_file = os.path.join(scratch_dir, "csv", f"prefeito_{pref_aid}_{ano}.csv")
            if os.path.exists(p_file) and os.path.getsize(p_file) > 50:
                continue
            raw_pref = fetch_csv_from_spl(f"{spl_base}/consulta-producao.aspx?autor={pref_aid}&ano={ano}")
            if raw_pref:
                with open(p_file, "w", encoding="utf-8") as out:
                    out.write(raw_pref)

    # 3. Coletar sessões e PDFs de votação nominal
    url_ses = f"{spl_base}/sessoes.aspx"
    h_ses = http_get(url_ses).decode("utf-8", errors="ignore")
    vs = re.search(r'id="__VIEWSTATE" value="([^"]*)"', h_ses).group(1)
    vsg = re.search(r'id="__VIEWSTATEGENERATOR" value="([^"]*)"', h_ses).group(1)
    ev_m = re.search(r'id="__EVENTVALIDATION" value="([^"]*)"', h_ses)
    post_ses = {
        "__VIEWSTATE": vs,
        "__VIEWSTATEGENERATOR": vsg,
        "ctl00$ContentPlaceHolder1$ddl_tipo_sessao": "",
        "ctl00$ContentPlaceHolder1$ddl_sessao_arquivo_tipo": "",
        "ctl00$ContentPlaceHolder1$txt_data_inicio": "2025-01-01",
        "ctl00$ContentPlaceHolder1$txt_data_final": f"{cur_year}-12-31",
        "ctl00$ContentPlaceHolder1$btn_buscar": "   FILTRAR   ",
    }
    if ev_m:
        post_ses["__EVENTVALIDATION"] = ev_m.group(1)
    page_html = (
        urllib.request.urlopen(
            urllib.request.Request(url_ses, data=urllib.parse.urlencode(post_ses).encode(), headers={"User-Agent": "Mozilla/5.0"}),
            timeout=30,
            context=SSL_CTX,
        )
        .read()
        .decode("utf-8", errors="ignore")
    )
    tp_m = re.search(r'id="ContentPlaceHolder1_lvSessoes_TotalPages" value="(\d+)"', page_html)
    tot_pages = int(tp_m.group(1)) if tp_m else 1
    pages = [page_html]
    cur_html = page_html
    for p in range(1, min(tot_pages, 35)):
        vs_m = re.search(r'id="__VIEWSTATE" value="([^"]*)"', cur_html)
        if not vs_m:
            break
        vsg = re.search(r'id="__VIEWSTATEGENERATOR" value="([^"]*)"', cur_html).group(1)
        ev_m = re.search(r'id="__EVENTVALIDATION" value="([^"]*)"', cur_html)
        post_data = {
            "__EVENTTARGET": "ctl00$ContentPlaceHolder1$lvSessoes_lbNext",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs_m.group(1),
            "__VIEWSTATEGENERATOR": vsg,
            "ctl00$ContentPlaceHolder1$ddl_tipo_sessao": "",
            "ctl00$ContentPlaceHolder1$ddl_sessao_arquivo_tipo": "",
            "ctl00$ContentPlaceHolder1$txt_data_inicio": "2025-01-01",
            "ctl00$ContentPlaceHolder1$txt_data_final": f"{cur_year}-12-31",
            "ctl00$ContentPlaceHolder1$lvSessoes_TotalPages": str(tot_pages),
            "ctl00$ContentPlaceHolder1$lvSessoes_CurrentPage": str(p - 1),
        }
        if ev_m:
            post_data["__EVENTVALIDATION"] = ev_m.group(1)
        cur_html = (
            urllib.request.urlopen(
                urllib.request.Request(url_ses, data=urllib.parse.urlencode(post_data).encode(), headers={"User-Agent": "Mozilla/5.0"}),
                timeout=30,
                context=SSL_CTX,
            )
            .read()
            .decode("utf-8", errors="ignore")
        )
        pages.append(cur_html)

    sessoes = []
    seen_ids = set()
    pdf_tasks = []
    for phtml in pages:
        for tr in re.findall(r"<tr>\s*<td>(.*?)</td>\s*</tr>", phtml, re.S):
            t_m = re.search(
                r"<strong>\s*(Ordinária|Extraordinária|Solene|Sessão Ordinária|Sessão Extraordinária|Sessão Solene)\s*N[°º]\s*(\d+)\s*</strong>",
                tr,
                re.S | re.I,
            )
            if not t_m:
                continue
            tipo_s = re.sub(r"^Sessão\s+", "", " ".join(t_m.group(1).split()), flags=re.I)
            num_s = t_m.group(2)
            d_m = re.search(r"<strong>Data:\s*</strong>\s*(\d{2}/\d{2}/\d{4})", tr)
            dt_str = d_m.group(1) if d_m else ""
            if not dt_str:
                continue
            pres_m = re.search(r"sessao-parlamentares\.aspx\?id=(\d+)", tr)
            ses_id_m = re.search(r"(?:sessao-parlamentares\.aspx\?id=|sessao-leitura\.aspx\?id=|/SES/)(\d+)", tr)
            ses_id = ses_id_m.group(1) if ses_id_m else f"{tipo_s}_{num_s}_{dt_str}"
            if ses_id in seen_ids:
                continue
            seen_ids.add(ses_id)
            vot_pdfs = re.findall(
                r"href=['\"](https?://[^'\"]+/Arquivo/Documents/SES/[^'\"]+)['\"][^>]*>\s*(?:Lista de Votações Nominais|Extrato da Votação Eletrônica|Resultado e Extrato da Votação)[^<]*</a>",
                tr,
                re.I,
            )
            sessoes.append({
                "id": ses_id,
                "tipo": tipo_s,
                "num": num_s,
                "data": dt_str,
                "has_presenca": bool(pres_m),
                "vot_pdfs": vot_pdfs,
            })
            dt_iso = f"{dt_str[6:10]}-{dt_str[3:5]}-{dt_str[0:2]}"
            for purl in vot_pdfs:
                pdf_tasks.append((ses_id, dt_iso, purl))

    def fetch_and_parse_pdf(task):
        ses_id, dt_iso, purl = task
        try:
            raw_pdf = http_get(purl, timeout=30)
            return parse_pdf_votes(raw_pdf, ses_id, dt_iso, purl)
        except Exception:
            return []

    all_votes = []
    if pdf_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for parsed_list in ex.map(fetch_and_parse_pdf, pdf_tasks):
                all_votes.extend(parsed_list)

    with open(os.path.join(scratch_dir, "vereadores_raw.json"), "w", encoding="utf-8") as f:
        json.dump(vereadores, f, ensure_ascii=False, indent=2)
    with open(os.path.join(scratch_dir, "sessoes_raw.json"), "w", encoding="utf-8") as f:
        json.dump(sessoes, f, ensure_ascii=False, indent=2)
    with open(os.path.join(scratch_dir, "votacoes_parsed.json"), "w", encoding="utf-8") as f:
        json.dump(all_votes, f, ensure_ascii=False, indent=2)


def build_spl_rx(cfg: dict, scratch_dir: str) -> dict:
    spl_base = cfg["spl_base"].rstrip("/")
    tse_city = load_tse_city(cfg["slug"])

    with open(os.path.join(scratch_dir, "vereadores_raw.json"), encoding="utf-8") as f:
        vereadores_raw = json.load(f)
    with open(os.path.join(scratch_dir, "sessoes_raw.json"), encoding="utf-8") as f:
        sessoes_raw = json.load(f)
    with open(os.path.join(scratch_dir, "votacoes_parsed.json"), encoding="utf-8") as f:
        votacoes_raw = json.load(f)

    vereadores_raw.sort(key=lambda x: norm(x["ap"]))

    sess_list = []
    semreg_list = []
    for s in sessoes_raw:
        if s["tipo"] not in ("Ordinária", "Extraordinária", "Solene"):
            continue
        d_str = s["data"]
        dt_iso = f"{d_str[6:10]}-{d_str[3:5]}-{d_str[0:2]}"
        if s["has_presenca"]:
            sess_list.append({"d": dt_iso})
        else:
            semreg_list.append({"d": dt_iso, "n": f"{s['num']}ª Sessão {s['tipo']}"})

    sess_list.sort(key=lambda x: x["d"])
    semreg_list.sort(key=lambda x: x["d"], reverse=True)
    ult_sessao = sess_list[-1]["d"] if sess_list else "2026-11-12"

    ap_to_idx = {}
    for idx, v in enumerate(vereadores_raw):
        ap_to_idx[norm(v["ap"])] = idx
        ap_to_idx[norm(clean_ap(v["ap"]))] = idx
        ap_to_idx[norm(v.get("nome_civil", ""))] = idx

    props_by_id = {}
    for path in sorted(glob.glob(os.path.join(scratch_dir, "csv", "autor_*.csv"))):
        fname = os.path.basename(path)
        m_f = re.match(r"autor_(\d+)_(\d{4})\.csv", fname)
        aid_file = m_f.group(1) if m_f else ""
        owner_idx = None
        for idx, v in enumerate(vereadores_raw):
            if str(v.get("aid")) == aid_file:
                owner_idx = idx
                break
        if owner_idx is None:
            continue
        with open(path, encoding="utf-8") as f:
            rows = parse_csv_content(f.read())
        for r in rows:
            if r["ano"] not in ("2025", "2026", "2027", "2028"):
                continue
            pid = r["id"]
            if pid not in props_by_id:
                props_by_id[pid] = {"row": r, "authors": set()}
            props_by_id[pid]["authors"].add(owner_idx)
            for a_part in r["autor"].split(","):
                na = norm(clean_ap(a_part.strip()))
                if na in ap_to_idx:
                    props_by_id[pid]["authors"].add(ap_to_idx[na])

    pref_props_by_id = {}
    for path in sorted(glob.glob(os.path.join(scratch_dir, "csv", "prefeito_*.csv"))):
        with open(path, encoding="utf-8") as f:
            rows = parse_csv_content(f.read())
        for r in rows:
            if r["ano"] not in ("2025", "2026", "2027", "2028"):
                continue
            if r["tipo"] == "Documentos Diversos":
                continue
            pref_props_by_id[r["id"]] = r

    prop_code_to_id = {}
    for pid, r in pref_props_by_id.items():
        abbr = TIPO_ABBR.get(r["tipo"], r["tipo"][:3].upper())
        prop_code_to_id[f"{abbr} {r['num']}/{r['ano']}"] = pid
        prop_code_to_id[norm(f"{r['tipo']} nº {r['num']}/{r['ano']}")] = pid

    for pid, pinfo in props_by_id.items():
        r = pinfo["row"]
        abbr = TIPO_ABBR.get(r["tipo"], r["tipo"][:3].upper())
        prop_code_to_id[f"{abbr} {r['num']}/{r['ano']}"] = pid
        prop_code_to_id[norm(f"{r['tipo']} nº {r['num']}/{r['ano']}")] = pid

    vote_result_by_pid = {}
    for vt in votacoes_raw:
        ntitle = norm(vt["prop_title"])
        matched_pid = prop_code_to_id.get(ntitle)
        if not matched_pid:
            m_t = re.search(
                r"(projeto de lei ordinaria|projeto de lei complementar|projeto de lei|projeto de decreto legislativo|projeto de resolucao|mocao|requerimento|emenda|substitutivo)\s*n[ºo°]?\s*(\d+/\d{4})",
                ntitle,
            )
            if m_t:
                t_key = m_t.group(1)
                matched_pid = (
                    prop_code_to_id.get(f"{t_key} nº {m_t.group(2)}")
                    or prop_code_to_id.get(f"projeto de lei ordinária nº {m_t.group(2)}")
                    or prop_code_to_id.get(f"PL {m_t.group(2)}")
                )
        if matched_pid and vt["resultado"]:
            res_str = "Aprovado" if "APROVAD" in vt["resultado"].upper() else ("Rejeitado" if "REJEITAD" in vt["resultado"].upper() else "")
            if res_str:
                vote_result_by_pid[matched_pid] = res_str
        vt["matched_pid"] = matched_pid

    catot = {"b": 0, "c": 0, "h": 0, "l": 0, "o": 0}
    v_cats = [{"b": 0, "c": 0, "h": 0, "l": 0, "o": 0} for _ in vereadores_raw]
    v_cob_pend = [0 for _ in vereadores_raw]
    coaut_counts = [Counter() for _ in vereadores_raw]
    cob_tot_all = 0
    cob_pend_all = 0
    cob_resp_all = 0

    def sort_key(item_tuple):
        pid, pinfo = item_tuple
        d = pinfo["row"]["data"]
        aaaammdd = f"{d[6:10]}{d[3:5]}{d[0:2]}"
        return (aaaammdd, int(pid) if pid.isdigit() else 0)

    sorted_props = sorted(props_by_id.items(), key=sort_key, reverse=True)
    items = []
    for pid, pinfo in sorted_props:
        r = pinfo["row"]
        auts = sorted(pinfo["authors"])
        if not auts:
            continue
        abbr = TIPO_ABBR.get(r["tipo"], r["tipo"][:3].upper())
        code = f"{abbr} {r['num']}/{r['ano']}"
        d = r["data"]
        aaaammdd = f"{d[6:10]}{d[3:5]}{d[0:2]}"
        grupo, eh_materia, th = classify_item(r["tipo"], r["ementa"])

        res_label = vote_result_by_pid.get(pid, "")
        if not res_label:
            if r["fase"] == "Retirar Proposição" or "retirad" in r["fase"].lower():
                res_label = "Pedido de Retirada"
            elif r["situacao"] == "Arquivado" and r["tipo"] in ("Moção", "Requerimento"):
                res_label = "Concluído"
            elif r["situacao"] == "Arquivado" and grupo == "l":
                res_label = "Concluído"

        if res_label in ("Rejeitado", "Pedido de Retirada"):
            st_code = "r"
        elif r["situacao"] == "Tramitando" and (
            "prefeitura" in r["setor"].lower()
            or "executivo" in r["setor"].lower()
            or "prestar informações" in r["fase"].lower()
            or "aguardar resposta" in r["fase"].lower()
        ):
            st_code = "p"
            if not res_label:
                res_label = "Aguardando Prefeitura"
        elif r["situacao"] == "Tramitando" and res_label != "Aprovado":
            st_code = "t"
        else:
            st_code = "a"

        if grupo == "c":
            cob_tot_all += 1
            if st_code == "p":
                cob_pend_all += 1
                for a_idx in auts:
                    v_cob_pend[a_idx] += 1
            elif st_code == "a":
                cob_resp_all += 1

        if len(auts) > 1:
            for i_a in auts:
                for j_a in auts:
                    if i_a != j_a:
                        coaut_counts[i_a][j_a] += 1

        items.append([pid, code, r["ementa"], aaaammdd, grupo, res_label, auts, eh_materia, th, st_code])
        catot[grupo] += 1
        for a_idx in auts:
            v_cats[a_idx][grupo] += 1

    pref_items = []
    pref_plo = 0
    pref_plc = 0
    pref_ret = 0
    pref_aprov = 0
    pref_tram = 0
    pref_rej = 0
    vetos_from_csv = []

    sorted_pref = sorted(
        pref_props_by_id.items(),
        key=lambda kv: (f"{kv[1]['data'][6:10]}{kv[1]['data'][3:5]}{kv[1]['data'][0:2]}", int(kv[0]) if kv[0].isdigit() else 0),
        reverse=True,
    )
    for pid, r in sorted_pref:
        abbr = TIPO_ABBR.get(r["tipo"], "MSG" if "Mensagem" in r["tipo"] else r["tipo"][:3].upper())
        code = f"{abbr} {r['num']}/{r['ano']}"
        d = r["data"]
        aaaammdd = f"{d[6:10]}{d[3:5]}{d[0:2]}"
        if r["tipo"] in ("Projeto de Lei Ordinária", "Projeto de Lei"):
            pref_plo += 1
        elif r["tipo"] == "Projeto de Lei Complementar":
            pref_plc += 1
        elif "Mensagem" in r["tipo"] or "Retirada" in r["tipo"]:
            pref_ret += 1
        elif "Veto" in r["tipo"]:
            v_st = "Mantido" if "mantid" in (r["situacao"] + " " + r["fase"]).lower() else ("Derrubado" if "rejeitad" in (r["situacao"] + " " + r["fase"]).lower() else "Em apreciação")
            vetos_from_csv.append({
                "t": f"Veto {r['num']}/{r['ano']}",
                "d": f"{d[6:10]}-{d[3:5]}-{d[0:2]}",
                "e": r["ementa"][:280],
                "aut": "Câmara Municipal",
                "status": v_st,
                "s": 0, "n": 0, "a": 0,
                "u": f"{spl_base}/processo.aspx?id={pid}",
                "vs": [], "vn": [], "va": [],
            })

        res_l = vote_result_by_pid.get(pid, "")
        if not res_l:
            if "retirad" in r["fase"].lower() or "Mensagem" in r["tipo"] or "Retirada" in r["tipo"]:
                res_l = "Retirada pelo Prefeito"
            elif r["situacao"] == "Arquivado":
                res_l = "Aprovado / Sancionado"
            else:
                res_l = "Em tramitação"

        if "Aprovado" in res_l:
            st_p = "a"
            pref_aprov += 1
        elif "Rejeitado" in res_l or "Retirad" in res_l:
            st_p = "r"
            pref_rej += 1
        else:
            st_p = "t"
            pref_tram += 1

        pref_items.append([pid, code, r["ementa"], aaaammdd, abbr, res_l, st_p])

    nvot = len(votacoes_raw)
    unan = 0
    v_votes = [{"vs": 0, "vn": 0, "va": 0, "naos": [], "alin_s": 0, "alin_t": 0} for _ in vereadores_raw]
    n_ver = len(vereadores_raw)
    vmat = [[[0, 0] for _ in range(n_ver)] for _ in range(n_ver)]
    rachas_list = []
    vetos_by_title = {}

    for vt in votacoes_raw:
        active_vals = [x for x in vt["votos"].values() if x in ("Sim", "Não", "Abstenção")]
        is_unan = bool(active_vals and len(set(active_vals)) == 1)
        if is_unan:
            unan += 1

        short_t = vt["prop_title"]
        short_t = re.sub(r"Projeto de Lei Ordinária\s*n[º°]?\s*", "PL ", short_t, flags=re.I)
        short_t = re.sub(r"Projeto de Lei Complementar\s*n[º°]?\s*", "PLC ", short_t, flags=re.I)
        short_t = re.sub(r"Projeto de Lei\s*n[º°]?\s*", "PL ", short_t, flags=re.I)
        short_t = re.sub(r"Projeto de Decreto Legislativo\s*n[º°]?\s*", "PDL ", short_t, flags=re.I)
        short_t = re.sub(r"Projeto de Resolução\s*n[º°]?\s*", "PRE ", short_t, flags=re.I)
        short_t = re.sub(r"Proposta de Emenda à Lei Orgânica\s*n[º°]?\s*", "PEL ", short_t, flags=re.I)
        short_t = re.sub(r"Moç(?:ão|ões)\s*n[º°]?\s*", "MOC ", short_t, flags=re.I)
        short_t = re.sub(r"Requerimentos?\s*n[º°]?\s*", "REQ ", short_t, flags=re.I)

        link_u = f"{spl_base}/processo.aspx?id={vt['matched_pid']}" if vt.get("matched_pid") else vt["url"]
        ementa_v = vt["ementa"] or f"{vt['prop_title']} ({vt['sess_name']} — {vt['turno']})"
        autoria_v = clean_ap(vt["autoria"] or "")

        is_veto = "veto" in vt["prop_title"].lower()
        is_pref_bill = any(k in (vt["autoria"] or "").lower() for k in ("prefeito", "executivo")) and not any(
            k in vt["prop_title"].lower() for k in ("emenda", "substitutivo", "subemenda")
        )
        is_pref_matter = is_veto or is_pref_bill

        sim_indices = []
        nao_indices = []
        abst_indices = []
        ver_vote_map = {}

        for pname, voto_str in vt["votos"].items():
            idx = ap_to_idx.get(norm(clean_ap(pname)))
            if idx is None:
                continue
            if voto_str in ("Sim", "Não", "Abstenção"):
                ver_vote_map[idx] = voto_str
                if is_pref_matter:
                    v_votes[idx]["alin_t"] += 1
                    if voto_str == "Sim":
                        v_votes[idx]["alin_s"] += 1

            if voto_str == "Sim":
                v_votes[idx]["vs"] += 1
                sim_indices.append(idx)
            elif voto_str == "Não":
                v_votes[idx]["vn"] += 1
                nao_indices.append(idx)
                v_votes[idx]["naos"].append({
                    "t": short_t, "d": vt["data"], "e": ementa_v[:280], "u": link_u, "v": "nao", "a": autoria_v
                })
            elif voto_str == "Abstenção":
                v_votes[idx]["va"] += 1
                abst_indices.append(idx)
                v_votes[idx]["naos"].append({
                    "t": short_t, "d": vt["data"], "e": ementa_v[:280], "u": link_u, "v": "abst", "a": autoria_v
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
            res_up = (vt["resultado"] or "").upper()
            v_status = "Mantido" if ("MANTID" in res_up or "APROVAD" in res_up) else ("Derrubado" if ("DERRUBAD" in res_up or "REJEITAD" in res_up) else "Votado")
            vetos_by_title[vt["prop_title"]] = {
                "t": short_t, "d": vt["data"], "e": ementa_v[:280], "aut": autoria_v or "Câmara Municipal",
                "status": v_status, "s": len(sim_indices), "n": len(nao_indices), "a": len(abst_indices),
                "u": link_u, "vs": sim_indices, "vn": nao_indices, "va": abst_indices,
            }

        if not is_unan and (nao_indices or abst_indices):
            res_up = (vt["resultado"] or "").upper()
            res_clean = "Aprovado" if "APROVAD" in res_up else ("Rejeitado" if "REJEITAD" in res_up else (vt["resultado"] or ""))
            rachas_list.append({
                "t": short_t, "d": vt["data"], "turno": vt.get("turno", ""), "e": ementa_v[:280],
                "aut": autoria_v or "Poder Executivo", "res": res_clean, "u": link_u,
                "s": sim_indices, "n": nao_indices, "a": abst_indices, "pref": 1 if is_pref_matter else 0,
            })

    rachas_list.sort(key=lambda x: (min(len(x["s"]), len(x["n"]) + len(x["a"])), len(x["n"]) + len(x["a"]), x["d"]), reverse=True)
    vetos_list = sorted(vetos_by_title.values(), key=lambda x: x["d"], reverse=True) or vetos_from_csv
    vetos_mant = sum(1 for x in vetos_list if x["status"] == "Mantido")
    vetos_derr = sum(1 for x in vetos_list if x["status"] == "Derrubado")

    v_out = []
    for idx, v in enumerate(vereadores_raw):
        pid = str(v["pid"])
        freq = v.get("freq", {})
        def sum_f(k):
            return sum(int(val) for val in freq.get(k, {}).values() if str(val).isdigit())

        P = sum_f("Presente")
        A = sum_f("Falta Justificada") + sum_f("Ausente com Justificativa") + sum_f("Licenciado") + sum_f("Afastado")
        F = sum_f("Falta") + sum_f("Ausente")

        mesa = ""
        mr = v.get("mesa_raw", "")
        m_cargo = re.search(r"(Presidente|1º Vice-Presidente|2º Vice-Presidente|Vice-presidente|Vice-Presidente|1º Secretário|2º Secretário|1ª Secretária|2ª Secretária)", mr, re.I)
        if m_cargo:
            mesa = m_cargo.group(1)

        tse_m = match_tse_vereador(v["ap"], v.get("nome_civil", ""), tse_city["vereadores"])
        votos_2024 = tse_m.get("votos24", 0)
        if not votos_2024:
            votos_matches = re.findall(r"(\d[\d\.]*)\s*votos", v.get("bio", ""), re.I)
            if votos_matches:
                votos_2024 = int(votos_matches[-1].replace(".", ""))

        tel_raw = v.get("tel", "").split("/")[0].strip()
        naos_sorted = sorted(v_votes[idx]["naos"], key=lambda x: x["d"], reverse=True)

        legs_raw = v.get("legislaturas", [])
        seen_leg_names = []
        mand_periods = []
        years_ini = []
        for lg in legs_raw:
            lname = lg.get("leg", "")
            if lname and lname not in seen_leg_names:
                seen_leg_names.append(lname)
                y0 = lg.get("ini", "")[-4:]
                y1 = lg.get("fim", "")[-4:]
                if y0.isdigit():
                    years_ini.append(int(y0))
                if y0 and y1:
                    mand_periods.append(f"{y0}–{y1}")

        mand_count = len(seen_leg_names) if seen_leg_names else 1
        desde_ano = min(years_ini) if years_ini else 2025
        mand_hist = list(reversed(mand_periods)) if mand_periods else ["2025–2028"]
        mand_txt = f"{mand_count}º mandato · desde {desde_ano}"

        leis_dict = v.get("leis", {})
        leis_tot = sum(leis_dict.values())
        leis_ord = (
            leis_dict.get("Lei Ordinária", 0)
            + leis_dict.get("Lei Complementar", 0)
            + leis_dict.get("Emenda à Lei Orgânica", 0)
        )

        coms_out = [
            {"c": c.get("cargo", ""), "n": re.sub(r"^Comissão\s+(?:Permanente\s+|Especial\s+)?de\s+", "", c.get("nome", ""))}
            for c in v.get("comissoes", [])
        ]
        em_imp_out = [
            {"a": e.get("ano", ""), "u": e.get("url", "")}
            for e in v.get("emendas_impositivas", [])
            if e.get("ano") in ("2026", "2025", "2024")
        ]

        alin_s = v_votes[idx]["alin_s"]
        alin_t = v_votes[idx]["alin_t"]
        alin_pct = round(100 * alin_s / alin_t) if alin_t > 0 else 0
        top_parc = [[other_i, cnt] for other_i, cnt in coaut_counts[idx].most_common(3)]

        gasto24 = tse_m.get("gasto24", 0.0)
        rec24 = tse_m.get("rec24", 0.0)
        gasto20 = tse_m.get("gasto20", 0.0)
        cpv24 = round(gasto24 / votos_2024, 2) if votos_2024 > 0 else 0.0

        v_out.append({
            "id": pid,
            "nome": v.get("nome_civil") or tse_m.get("civil", "").title() or v["ap"],
            "ap": v["ap"],
            "part": v["part"] or tse_m.get("part24", ""),
            "votos": votos_2024,
            "ini": "2025-01-01",
            "mesa": mesa,
            "P": P,
            "A": A,
            "F": F,
            "email": v.get("email", ""),
            "tel": tel_raw,
            "whats": "",
            "vs": v_votes[idx]["vs"],
            "vn": v_votes[idx]["vn"],
            "va": v_votes[idx]["va"],
            "naos": naos_sorted,
            "cat": v_cats[idx],
            "url": f"{spl_base}/parlamentar.aspx?id={pid}",
            "foto": v.get("foto", 0),
            "mand": mand_count,
            "desde": desde_ano,
            "mand_txt": mand_txt,
            "mand_hist": mand_hist,
            "nota_exec": "",
            "leis_tot": leis_tot,
            "leis_ord": leis_ord,
            "leis_det": leis_dict,
            "coms": coms_out,
            "em_imp": em_imp_out,
            "alin": alin_pct,
            "alin_s": alin_s,
            "alin_t": alin_t,
            "parc": top_parc,
            "cob_pend": v_cob_pend[idx],
            "rec24": rec24,
            "gasto24": gasto24,
            "cpv24": cpv24,
            "gasto20": gasto20,
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
        "semreg": semreg_list,
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
            "ret": pref_ret,
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
