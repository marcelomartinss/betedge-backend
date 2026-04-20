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

# ─────────────────────────────────────────────
# NBA — ESPN API aberta (sem key, sem bloqueio)
# ─────────────────────────────────────────────
def fetch_nba_jogos():
    log("Buscando jogos NBA...")
    try:
        today = datetime.now(BRASILIA).strftime("%Y%m%d")
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today}"
        r = requests.get(url, headers=HEADERS, timeout=20)
        data = r.json()
        events = data.get("events", [])
        log(f"  {len(events)} jogos NBA encontrados")

        supabase.table("jogos_hoje").delete().eq("sport", "basquete").execute()

        for ev in events:
            competitions = ev.get("competitions", [{}])
            comp = competitions[0] if competitions else {}
            competitors = comp.get("competitors", [])

            home = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "home"), "?")
            away = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "away"), "?")
            home_abbr = next((c.get("team", {}).get("abbreviation", "") for c in competitors if c.get("homeAway") == "home"), "")
            away_abbr = next((c.get("team", {}).get("abbreviation", "") for c in competitors if c.get("homeAway") == "away"), "")

            # Horário em Brasília
            date_str = ev.get("date", "")
            horario_br = "?"
            try:
                dt_utc = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=pytz.utc)
                dt_br = dt_utc.astimezone(BRASILIA)
                horario_br = dt_br.strftime("%H:%M")
            except:
                pass

            status = ev.get("status", {}).get("type", {}).get("name", "agendado")
            serie_info = comp.get("series", {})
            serie_txt = ""
            if serie_info:
                wins_home = serie_info.get("competitors", [{}])[0].get("wins", 0) if serie_info.get("competitors") else 0
                wins_away = serie_info.get("competitors", [{}])[1].get("wins", 0) if len(serie_info.get("competitors", [])) > 1 else 0
                serie_txt = f"Série: {away_abbr} {wins_away}-{wins_home} {home_abbr}"

            # Lesões
            ausencias = buscar_lesoes_espn_nba(home, away)

            jogo = {
                "sport": "basquete",
                "liga": "NBA Playoffs" if serie_txt else "NBA",
                "time_casa": home,
                "time_fora": away,
                "horario_brasilia": horario_br,
                "status": "agendado",
                "ausencias": json.dumps(ausencias),
                "odds": json.dumps({"serie": serie_txt}),
                "updated_at": datetime.now(BRASILIA).isoformat(),
            }
            result = supabase.table("jogos_hoje").insert(jogo).execute()
            jogo_id = result.data[0]["id"] if result.data else None

            if ausencias and jogo_id:
                for aus in ausencias:
                    existing = supabase.table("alertas").select("id")\
                        .eq("tipo", "ausencia")\
                        .ilike("titulo", f"%{aus['jogador']}%")\
                        .execute()
                    if not existing.data:
                        supabase.table("alertas").insert({
                            "tipo": "ausencia",
                            "titulo": f"AUSENCIA NBA - {aus['jogador']} ({aus['time']})",
                            "descricao": f"{aus['jogador']} fora para {away} @ {home}. Status: {aus.get('status','?')}. {serie_txt}",
                            "jogo_id": jogo_id,
                            "sport": "basquete",
                            "prioridade": "alta",
                            "fonte": "ESPN",
                        }).execute()
                        log(f"  ALERTA AUSENCIA: {aus['jogador']} - {aus['time']}")

            log(f"  NBA: {away} @ {home} {horario_br} {serie_txt}")

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
            home_words = home.split()[:2]
            away_words = away.split()[:2]
            if any(w in team_name for w in home_words) or any(w in team_name for w in away_words):
                for inj in item.get("injuries", []):
                    status = inj.get("status", "")
                    if status in ["Out", "Doubtful", "Questionable"]:
                        athlete = inj.get("athlete", {}).get("displayName", "?")
                        lesoes.append({"jogador": athlete, "time": team_name, "status": status})
    except Exception as e:
        log(f"  Erro lesoes NBA: {e}")
    return lesoes

# ─────────────────────────────────────────────
# FUTEBOL — TheSportsDB
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# MATCHUPS IA — análise profunda playoffs
# ─────────────────────────────────────────────
def gerar_matchups_ia():
    log("Gerando matchups com IA...")
    try:
        jogos = supabase.table("jogos_hoje").select("*").execute()
        if not jogos.data:
            log("  Sem jogos")
            return

        nba_jogos = [j for j in jogos.data if j["sport"] == "basquete"]
        fut_jogos = [j for j in jogos.data if j["sport"] == "futebol"]

        resumo_nba = [f"NBA PLAYOFFS | {j['liga']} | {j['time_fora']} @ {j['time_casa']} | {j['horario_brasilia']} | Ausencias: {j.get('ausencias','[]')}" for j in nba_jogos]
        resumo_fut = [f"FUTEBOL | {j['liga']} | {j['time_fora']} @ {j['time_casa']} | {j['horario_brasilia']}" for j in fut_jogos[:6]]

        hoje = datetime.now(BRASILIA).strftime("%d/%m/%Y")
        prompt = f"""Voce e um analista especialista em VALUE BETS com odds altas.

Hoje e {hoje}. FASE DE PLAYOFFS NBA — momento ideal para props de jogadores.

JOGOS NBA HOJE:
{chr(10).join(resumo_nba) if resumo_nba else "Sem jogos NBA"}

JOGOS FUTEBOL HOJE:
{chr(10).join(resumo_fut) if resumo_fut else "Sem jogos futebol"}

ANALISE OS 3-5 MELHORES MERCADOS DE VALOR para hoje.

Para NBA Playoffs, considere:
- Props de jogadores: pontos, rebotes, assistencias, trios
- Impacto de ausencias nas props dos outros jogadores
- Tendencias da serie atual (quem esta dominando)
- Minutagem esperada em playoffs (mais intensa)
- Historico do jogador em playoffs vs temporada regular

Para futebol considere:
- Escanteios 1T over/under (media dos times)
- Finalizacoes jogador/time no 1T
- Desarmes (quem marca quem, mapa de calor)
- Chutes fora da area (times que cedem/executam)

Para cada entrada retorne:

**MERCADO:** descricao completa
**JOGO:** time A vs time B
**FUNDAMENTO:** dados, tendencias, contexto
**ODD ESPERADA:** valor
**CONFIANCA:** 1-10
**STAKE:** % da banca (0.5% big odd acima de 4.0, 1-2% odds menores)
**TIMING:** se precisa confirmar algo antes (escalacao, ausencia)

Seja cirurgico. Prefira qualidade a quantidade. Odds acima de 2.0 com fundamento solido."""

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
            "sport": "geral",
            "prioridade": "normal",
            "fonte": "BetEdge IA",
            "lido": False,
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
