#!/usr/bin/env python3
"""
Orquestrador mestre do Radar Cidadão (Mandato 2025–2028).
Executa a coleta oficial, cruzamento com o TSE (2012–2026), validação constitucional
de cadeiras e geração de `dados.js`, `index.html`, `logo.png` e `card-whatsapp.jpg`
para as 8 cidades monitoradas e para o Hub central na raiz.

Uso:
  python3 coletar.py                    # Atualiza todas as 8 cidades + Hub
  python3 coletar.py --cidade sp        # Atualiza apenas uma cidade + Hub
  python3 coletar.py --cidade sjc --cidade jacarei
"""

import json
import os
import shutil
import sys
import time

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from cidades_config import CIDADES
from gerar_artes import fmt_mil, generate_logo, generate_whatsapp_card, nf
from gerar_portais import (
    BASE_ORIGIN,
    CITY_NAV_ORDER,
    build_city_index_html,
    build_hub_html,
    load_rx,
)
from motor_caragua import collect_caraguatatuba
from motor_cmsp import collect_sp
from motor_jacarei import collect_jacarei
from motor_spl import build_spl_rx, collect_spl_city
from motor_tse_fallback import collect_tse_city

TAUBATE_REF_DIR = "/tmp/radartaubate_ref"


def save_rx_js(city_dir: str, rx: dict):
    os.makedirs(city_dir, exist_ok=True)
    out_path = os.path.join(city_dir, "dados.js")
    js = "window.RX=" + json.dumps(rx, ensure_ascii=False, separators=(",", ":")) + ";\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js)
    return len(js)


def collect_and_build_city(slug: str, force_live: bool = False) -> dict:
    cfg = CIDADES[slug]
    city_dir = os.path.join(ROOT_DIR, slug)
    fotos_dir = os.path.join(city_dir, "fotos")
    os.makedirs(fotos_dir, exist_ok=True)

    motor = cfg["motor"]
    t0 = time.time()

    if slug == "taubate" and not force_live and os.path.exists(os.path.join(TAUBATE_REF_DIR, "dados.js")):
        print(f"\n[TAUBATÉ] Carregando base oficial verificada do Radar Taubaté...")
        ref_fotos = os.path.join(TAUBATE_REF_DIR, "fotos")
        if os.path.isdir(ref_fotos):
            for fn in os.listdir(ref_fotos):
                if fn.endswith(".jpg"):
                    shutil.copy2(os.path.join(ref_fotos, fn), os.path.join(fotos_dir, fn))
        with open(os.path.join(TAUBATE_REF_DIR, "dados.js"), encoding="utf-8") as f:
            raw = f.read().strip()
        if raw.startswith("window.RX="):
            raw = raw[len("window.RX="):]
        if raw.endswith(";"):
            raw = raw[:-1]
        rx = json.loads(raw)
        rx["cfg"] = {
            "nome": cfg["nome"],
            "uf": cfg["uf"],
            "portal_url": cfg["portal_url"],
            "proc_url": cfg["proc_url"],
            "form_url": cfg["form_url"],
            "leis_url": cfg["leis_url"],
            "fonte_nome": cfg["fonte_nome"],
            "endereco": cfg["endereco"],
        }
    elif motor == "spl":
        scratch_dir = os.path.join("/tmp/radar_spl_cache", slug)
        os.makedirs(scratch_dir, exist_ok=True)
        if force_live or not os.path.exists(os.path.join(scratch_dir, "vereadores_raw.json")):
            collect_spl_city(cfg, city_dir, scratch_dir)
        rx = build_spl_rx(cfg, scratch_dir)
    elif motor == "cmsp":
        rx = collect_sp(cfg, city_dir)
    elif motor == "jacarei":
        rx = collect_jacarei(cfg, city_dir)
    elif motor == "caraguatatuba":
        rx = collect_caraguatatuba(cfg, city_dir)
    else:
        rx = collect_tse_city(cfg, city_dir)

    # Validação estrita do número constitucional de cadeiras antes de gravar dados.js
    n_ver = len(rx.get("v", []))
    if n_ver != cfg["cadeiras"]:
        raise RuntimeError(
            f"ABORTADO [{slug}]: Esperados {cfg['cadeiras']} vereadores, obtidos {n_ver}"
        )

    sz = save_rx_js(city_dir, rx)

    # Gerar index.html da cidade
    html_str = build_city_index_html(slug, rx, cfg)
    with open(os.path.join(city_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_str)

    # Gerar logo.png e card-whatsapp.jpg (< 300 KB) da cidade
    if slug == "taubate" and os.path.exists(os.path.join(TAUBATE_REF_DIR, "logo.png")):
        shutil.copy2(os.path.join(TAUBATE_REF_DIR, "logo.png"), os.path.join(city_dir, "logo.png"))
    else:
        generate_logo(os.path.join(city_dir, "logo.png"), cfg["logo_red"], cfg["logo_black"])

    tot_items = len(rx.get("items", []))
    catot = rx.get("catot", {})
    hom = catot.get("h", 0)
    pct_h = round(hom / tot_items * 100) if tot_items else 0
    tot_gasto24 = sum(v.get("gasto24", 0) or 0 for v in rx["v"])
    cand26 = [v for v in rx["v"] if any(c.get("ano") == 2026 for c in (v.get("outras_cand") or []))]
    tot_gasto26 = sum(
        next((c.get("desp", 0) for c in (v.get("outras_cand") or []) if c.get("ano") == 2026), 0)
        for v in cand26
    )
    cob_pend = (rx.get("pref") or {}).get("cob_pend", 0) or catot.get("c", 0)
    n_bairros = len(rx.get("bairros", []))

    if tot_items > 0:
        kpi1 = (nf(tot_items), f"ações em {n_bairros} bairros", "Filtre sua rua por período, situação e assunto")
        kpi2 = (f"{pct_h}%", "são só homenagens", f"{nf(hom)} moções de aplauso, pesar e títulos")
        kpi3 = (
            fmt_mil(tot_gasto24),
            "gastos na eleição 2024",
            f"Em 2026, {len(cand26)} tentaram Deputado ({fmt_mil(tot_gasto26)})" if cand26 else "Prestação de contas oficial no TSE",
        )
        kpi4 = (
            nf(cob_pend),
            "cobranças à Prefeitura",
            "Requerimentos de fiscalização enviados ao Executivo",
        )
    else:
        p_gasto = (rx.get("pref") or {}).get("gasto24", 0)
        kpi1 = (fmt_mil(tot_gasto24), f"gastos pelos {n_ver} eleitos", "Campanha de 2024 declarada no TSE")
        kpi2 = (str(len(cand26)), "candidatos a Deputado 2026", f"Gastaram {fmt_mil(tot_gasto26)} no meio do mandato" if cand26 else "Histórico TSE 2012–2026 completo")
        kpi3 = (fmt_mil(p_gasto), f"campanha {rx['pref']['nome']}", "Dados oficiais da eleição de Prefeito em 2024")
        kpi4 = (f"R$ {nf(rx['sal']['v'])}", "salário mensal vereador", rx["sal"]["lei"][:42])

    host_clean = BASE_ORIGIN.replace("https://", "").replace("http://", "")
    jpg_bytes = generate_whatsapp_card(
        os.path.join(city_dir, "card-whatsapp.jpg"),
        cfg["nome"],
        f"{host_clean}/{slug}",
        n_ver,
        kpi1,
        kpi2,
        kpi3,
        kpi4,
        is_hub=False,
    )

    dt_s = time.time() - t0
    print(
        f"  [OK] {cfg['nome']:22s} | {n_ver:2d} ver | {tot_items:5d} props | "
        f"dados.js={sz//1024:4d} KB | card={jpg_bytes//1024:3d} KB | {dt_s:.1f}s"
    )
    return rx


def build_root_hub():
    summaries = []
    for slug, label, seats in CITY_NAV_ORDER:
        city_dir = os.path.join(ROOT_DIR, slug)
        if not os.path.exists(os.path.join(city_dir, "dados.js")):
            continue
        rx = load_rx(city_dir)
        cfg = CIDADES[slug]
        tot_items = len(rx.get("items", []))
        catot = rx.get("catot", {})
        tot_gasto24 = sum(v.get("gasto24", 0) or 0 for v in rx["v"])
        cand26 = [v for v in rx["v"] if any(c.get("ano") == 2026 for c in (v.get("outras_cand") or []))]
        tot_gasto26 = sum(
            next((c.get("desp", 0) for c in (v.get("outras_cand") or []) if c.get("ano") == 2026), 0)
            for v in cand26
        )
        summaries.append({
            "slug": slug,
            "nome": cfg["nome"],
            "n_ver": len(rx["v"]),
            "pref_nome": rx["pref"]["nome"],
            "pref_part": rx["pref"]["part"],
            "tot_items": tot_items,
            "hom": catot.get("h", 0),
            "leis": catot.get("l", 0),
            "nvot": rx.get("nvot", 0),
            "tot_gasto24": tot_gasto24,
            "cand26": len(cand26),
            "tot_gasto26": tot_gasto26,
            "sal_v": rx["sal"]["v"],
        })

    hub_html = build_hub_html(summaries)
    with open(os.path.join(ROOT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(hub_html)

    generate_logo(os.path.join(ROOT_DIR, "logo.png"), "CIDA", "DÃO")

    tot_ver = sum(s["n_ver"] for s in summaries)
    tot_props = sum(s["tot_items"] for s in summaries)
    tot_gasto24 = sum(s["tot_gasto24"] for s in summaries)
    tot_cand26 = sum(s["cand26"] for s in summaries)
    tot_gasto26 = sum(s["tot_gasto26"] for s in summaries)
    host_clean = BASE_ORIGIN.replace("https://", "").replace("http://", "")

    generate_whatsapp_card(
        os.path.join(ROOT_DIR, "card-whatsapp.jpg"),
        "CIDADÃO",
        host_clean,
        tot_ver,
        (f"{len(summaries)} Cidades", f"{tot_ver} vereadores + 8 prefeitos", "Vale do Paraíba e Capital Paulista"),
        (nf(tot_props), "proposições analisadas", "Classificadas por categoria, tema e situação"),
        (fmt_mil(tot_gasto24), "gastos na eleição 2024", "Cruzamento oficial com o TSE (2012–2026)"),
        (f"{tot_cand26} vereadores", "candidatos a Deputado 2026", f"Gastaram {fmt_mil(tot_gasto26)} no meio do mandato"),
        is_hub=True,
    )
    print(f"\n[HUB RAIZ] /index.html, /logo.png e /card-whatsapp.jpg gerados ({len(summaries)} cidades).")


def main():
    args = sys.argv[1:]
    force_live = "--forcar-live" in args
    only_html = "--apenas-html" in args
    target_slugs = []
    for i, a in enumerate(args):
        if a == "--cidade" and i + 1 < len(args):
            target_slugs.append(args[i + 1].strip().lower())

    if not target_slugs:
        target_slugs = [slug for slug, _, _ in CITY_NAV_ORDER]

    for slug in target_slugs:
        if slug not in CIDADES:
            raise ValueError(f"Cidade desconhecida: {slug}. Opções: {list(CIDADES.keys())}")
        if only_html:
            city_dir = os.path.join(ROOT_DIR, slug)
            rx = load_rx(city_dir)
            html_str = build_city_index_html(slug, rx, CIDADES[slug])
            with open(os.path.join(city_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(html_str)
            print(f"  [HTML OK] {slug}/index.html regenerado a partir de dados.js")
        else:
            collect_and_build_city(slug, force_live=force_live)

    build_root_hub()


if __name__ == "__main__":
    main()
