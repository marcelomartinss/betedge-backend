import os
import json
import time
import schedule
import requests
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_KEY   = os.environ.get("GEMINI_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
BRASILIA = pytz.timezone("America/Sao_Paulo")

def log(msg):
    agora = datetime.now(BRASILIA).strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BetEdgeBot/1.0)", "Accept": "application/json"}

def fetch_nba_jogos():
    log("Buscando jogos NBA...")
    try:
        today = datetime.now(BRASILIA).strftime("%Y-%m-%d")
        url = f"https://api.balldontlie.io/v1/games?dates[]={today}&per_page=20"
        r = requests.get(url, headers=HEADERS, timeout=20)
        data = r.json()
        games = data.get("data", [])
        log(f"  {len(games)} jogos NBA encontrados")
        supabase.table("jogos_hoje").delete().eq("sport", "basquete").execute()
        for g in games:
            home = g.get("home_team", {}).get("full_name", "?")
            away = g.get("visitor_team", {}).get("full_name", "?")
            horario_br = "?"
            try:
                dt_str = g.get("date", "")
                if dt_str:
                    dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
                    horario_br = "NBA " + dt.strftime("%d/%m")
            except:
                pass
            ausencias = buscar_lesoes_espn_nba(home, away)
            jogo = {
                "sport": "basquete", "liga": "NBA",
                "time_casa": home, "time_fora": away,
                "horario_brasilia": horario_br, "status": "agendado",
                "ausencias": json.dumps(ausencias), "odds": json.dumps({}),
                "updated_at": datetime.now(BRASILIA).isoformat(),
            }
            result = supabase.table("jogos_hoje").insert(jogo).execute()
            jogo_id = result.data[0]["id"] if result.data else None
            if ausencias and jogo_id:
                for aus in ausencias:
                    existing = supabase.table("alertas").select("id").eq("tipo", "ausencia").ilike("titulo", f"%{aus['jogador']}%").execute()
                    if not existing.data:
                        supabase.table("alertas").insert({
                            "tipo": "ausencia",
                            "titulo": f"AUSENCIA NBA - {aus['jogador']} ({aus['time']})",
                            "descricao": f"{aus['jogador']} fora para {away} @ {home}. Status: {aus.get('status','?')}",
                            "jogo_id": jogo_id, "sport": "basquete",
                            "prioridade": "alta", "fonte": "ESPN",
                        }).execute()
                        log(f"  ALERTA: {aus['jogador']} ausente")
            log(f"  NBA: {away} @ {home}")
    except Exception as e:
        log(f"Erro NBA: {e}")

def buscar_lesoes_espn_nba(home, away):
    lesoes = []
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        for item in data.get("items", []):
            team_name = item.get("team", {}).get("displayName", "")
            if any(w in team_name for w in home.split()[:2]) or any(w in team_name for w in away.split()[:2]):
                for inj in item.get("injuries", []):
                    status = inj.get("status", "")
                    if status in ["Out", "Doubtful", "Questionable"]:
                        lesoes.append({"jogador": inj.get("athlete", {}).get("displayName", "?"), "time": team_name, "status": status})
    except Exception as e:
        log(f"  Erro lesoes ESPN: {e}")
    return lesoes

def fetch_futebol_jogos():
    log("Buscando jogos de futebol...")
    try:
        today = datetime.now(BRASILIA).strftime("%Y-%m-%d")
        supabase.table("jogos_hoje").delete().eq("sport", "futebol").execute()
        count = 0
        ligas = [
            ("4328", "Premier League"), ("4335", "La Liga"),
            ("4332", "Bundesliga"), ("4331", "Serie A"),
            ("4334", "Ligue 1"), ("4480", "Champions League"),
            ("4406", "Brasileirao"),
        ]
        for liga_id, liga_nome in ligas:
            try:
                url = f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={today}&l={liga_id}"
                r = requests.get(url, headers=HEADERS, timeout=15)
                data = r.json()
                eventos = data.get("events") or []
                for ev in eventos:
                    home = ev.get("strHomeTeam", "?")
                    away = ev.get("strAwayTeam", "?")
                    time_str = ev.get("strTime", "?")
                    horario_br = "?"
                    try:
                        if time_str and time_str != "?":
                            hh, mm = time_str[:5].split(":")
                            dt_utc = datetime.now(pytz.utc).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                            horario_br = dt_utc.astimezone(BRASILIA).strftime("%H:%M")
                    except:
                        horario_br = time_str[:5] if time_str else "?"
                    supabase.table("jogos_hoje").insert({
                        "sport": "futebol", "liga": liga_nome,
                        "time_casa": home, "time_fora": away,
                        "horario_brasilia": horario_br, "status": "agendado",
                        "ausencias": json.dumps([]), "odds": json.dumps({}),
                        "updated_at": datetime.now(BRASILIA).isoformat(),
                    }).execute()
                    count += 1
                time.sleep(0.5)
            except Exception as e:
                log(f"  Erro liga {liga_nome}: {e}")
                continue
        log(f"  {count} jogos de futebol salvos")
        if count > 0:
            fetch_escalacoes_futebol()
    except Exception as e:
        log(f"Erro futebol: {e}")

def fetch_escalacoes_futebol():
    log("Verificando escalacoes...")
    agora = datetime.now(BRASILIA)
    try:
        jogos = supabase.table("jogos_hoje").select("*").eq("sport", "futebol").eq("escalacao_confirmada", False).execute()
        for jogo in jogos.data:
            horario_str = jogo.get("horario_brasilia", "")
            if not horario_str or horario_str == "?":
                continue
            try:
                hh, mm = horario_str.split(":")
                dt_jogo = agora.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if dt_jogo < agora:
                    dt_jogo += timedelta(days=1)
                diff_min = (dt_jogo - agora).total_seconds() / 60
                if 0 < diff_min < 75:
                    existing = supabase.table("alertas").select("id").eq("jogo_id", jogo["id"]).eq("tipo", "escalacao_pendente").execute()
                    if not existing.data:
                        supabase.table("alertas").insert({
                            "tipo": "escalacao_pendente",
                            "titulo": f"ESCALACAO - {jogo['time_fora']} @ {jogo['time_casa']}",
                            "descricao": f"Jogo em {int(diff_min)}min ({jogo['liga']}). Confirme escalacao antes de entrar.",
                            "jogo_id": jogo["id"], "sport": "futebol",
                            "prioridade": "alta", "fonte": "BetEdge",
                        }).execute()
                        log(f"  Alerta escalacao: {jogo['time_casa']} vs {jogo['time_fora']}")
            except:
                continue
    except Exception as e:
        log(f"Erro escalacoes: {e}")

def gerar_matchups_ia():
    log("Gerando matchups com IA...")
    try:
        jogos = supabase.table("jogos_hoje").select("*").execute()
        if not jogos.data:
            log("  Sem jogos pra analisar")
            return
        resumo = [f"{j['sport'].upper()} | {j['liga']} | {j['time_fora']} @ {j['time_casa']} | {j['horario_brasilia']}" for j in jogos.data[:10]]
        hoje = datetime.now(BRASILIA).strftime("%d/%m/%Y")
        prompt = f"""Voce e um analista especialista em apostas esportivas de valor alto (VALUE BETS).

Hoje e {hoje}. Jogos:
{chr(10).join(resumo)}

Analise os 3 melhores matchups para apostas com ODDS ALTAS (2.0+).
Para cada um retorne:

**JOGO:** time A vs time B
**MERCADO:** mercado especifico (NAO resultado 1x2)
**FUNDAMENTO:** estatisticas e tendencias
**ODD ESPERADA:** valor
**CONFIANCA:** 1-10
**STAKE:** % da banca (max 2% normal, 0.5% big odd)

Foco futebol: Escanteios 1T, Finalizacoes, Desarmes, Chutes fora area
Foco NBA: Props pts/reb/ast, playoffs matchups

Sem fundamento = sem entrada."""
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60
        )
        data = r.json()
        analise = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "Sem analise")
        supabase.table("alertas").insert({
            "tipo": "matchup",
            "titulo": f"MATCHUPS DO DIA - {hoje}",
            "descricao": analise,
            "sport": "geral", "prioridade": "normal",
            "fonte": "BetEdge IA", "lido": False,
        }).execute()
        log("  Matchups salvos!")
    except Exception as e:
        log(f"Erro matchups: {e}")

def update_ultima_atualizacao():
    try:
        supabase.table("configuracoes").update({"valor": datetime.now(BRASILIA).isoformat()}).eq("chave", "ultima_atualizacao").execute()
    except Exception as e:
        log(f"Erro config: {e}")

def rotina_completa():
    log("=== ROTINA COMPLETA ===")
    fetch_nba_jogos()
    fetch_futebol_jogos()
    gerar_matchups_ia()
    update_ultima_atualizacao()
    log("=== FIM DA ROTINA ===")

def rotina_escalacoes():
    log("--- Rotina escalacoes ---")
    fetch_escalacoes_futebol()
    fetch_nba_jogos()
    update_ultima_atualizacao()

if __name__ == "__main__":
    log("BetEdge Backend iniciado")
    rotina_completa()
    schedule.every().day.at("07:00").do(rotina_completa)
    schedule.every().day.at("10:00").do(rotina_completa)
    schedule.every().day.at("13:00").do(rotina_escalacoes)
    schedule.every().day.at("15:00").do(rotina_escalacoes)
    schedule.every().day.at("17:00").do(rotina_escalacoes)
    schedule.every().day.at("19:00").do(rotina_escalacoes)
    schedule.every().day.at("21:00").do(rotina_escalacoes)
    schedule.every().day.at("23:00").do(rotina_escalacoes)
    schedule.every(30).minutes.do(rotina_escalacoes)
    log("Scheduler ativo")
    while True:
        schedule.run_pending()
        time.sleep(60)
