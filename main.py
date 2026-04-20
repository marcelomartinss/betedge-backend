import os
import json
import time
import schedule
import requests
import pytz
from datetime import datetime, date
from dotenv import load_dotenv
from supabase import create_client
from bs4 import BeautifulSoup

# NBA API
from nba_api.stats.endpoints import scoreboardv2, boxscoretraditionalv2
from nba_api.stats.static import teams as nba_teams_static
from nba_api.live.nba.endpoints import scoreboard as live_scoreboard

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_KEY = os.environ.get("GEMINI_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
BRASILIA = pytz.timezone("America/Sao_Paulo")

def log(msg):
    agora = datetime.now(BRASILIA).strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}")

# ─────────────────────────────────────────────
# NBA
# ─────────────────────────────────────────────
def fetch_nba_jogos():
    log("Buscando jogos NBA do dia...")
    try:
        today = datetime.now(BRASILIA).strftime("%Y-%m-%d")
        board = scoreboardv2.ScoreboardV2(game_date=today)
        games = board.game_header.get_data_frame()

        if games.empty:
            log("Nenhum jogo NBA hoje")
            return

        # Limpa jogos NBA do dia antes de reinserir
        supabase.table("jogos_hoje").delete().eq("sport", "basquete").execute()

        for _, g in games.iterrows():
            game_id   = str(g.get("GAME_ID", ""))
            home_team = g.get("HOME_TEAM_ID", "")
            away_team = g.get("VISITOR_TEAM_ID", "")

            # Resolve nome dos times
            all_teams = {t["id"]: t["full_name"] for t in nba_teams_static.get_teams()}
            home_name = all_teams.get(home_team, str(home_team))
            away_name = all_teams.get(away_team, str(away_team))

            # Horário em Brasília
            game_status = str(g.get("GAME_STATUS_TEXT", ""))
            game_time_utc = g.get("GAME_DATE_EST", "")

            ausencias = buscar_lesoes_nba(home_name, away_name)

            jogo = {
                "sport": "basquete",
                "liga": "NBA",
                "time_casa": home_name,
                "time_fora": away_name,
                "horario_brasilia": game_status,
                "status": "agendado",
                "ausencias": json.dumps(ausencias),
                "odds": json.dumps({}),
                "updated_at": datetime.now(BRASILIA).isoformat(),
            }
            result = supabase.table("jogos_hoje").insert(jogo).execute()
            jogo_id = result.data[0]["id"] if result.data else None

            # Gera alerta se há ausências importantes
            if ausencias and jogo_id:
                for aus in ausencias:
                    supabase.table("alertas").insert({
                        "tipo": "ausencia",
                        "titulo": f"AUSÊNCIA NBA — {aus['jogador']} ({aus['time']})",
                        "descricao": f"{aus['jogador']} está fora para {home_name} vs {away_name}. Status: {aus.get('status','?')}",
                        "jogo_id": jogo_id,
                        "sport": "basquete",
                        "prioridade": "alta",
                        "fonte": "ESPN/NBA",
                    }).execute()
                    log(f"  ALERTA: {aus['jogador']} ausente")

            log(f"  NBA: {away_name} @ {home_name}")

    except Exception as e:
        log(f"Erro NBA: {e}")

def buscar_lesoes_nba(time_casa, time_fora):
    """Busca lesões via ESPN injury report"""
    lesoes = []
    try:
        url = "https://www.espn.com/nba/injuries"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        # Procura por tabelas de lesões
        tables = soup.find_all("div", class_="Table__Scroller")
        for table in tables:
            rows = table.find_all("tr")
            time_atual = ""
            for row in rows:
                header = row.find("th")
                if header:
                    time_atual = header.get_text(strip=True)
                cells = row.find_all("td")
                if len(cells) >= 3:
                    jogador = cells[0].get_text(strip=True)
                    status = cells[2].get_text(strip=True) if len(cells) > 2 else "?"
                    if time_atual and (time_casa.split()[0] in time_atual or time_fora.split()[0] in time_atual):
                        if "Out" in status or "Doubtful" in status:
                            lesoes.append({
                                "jogador": jogador,
                                "time": time_atual,
                                "status": status
                            })
    except Exception as e:
        log(f"  Erro lesões ESPN: {e}")
    return lesoes

# ─────────────────────────────────────────────
# FUTEBOL
# ─────────────────────────────────────────────
def fetch_futebol_jogos():
    log("Buscando jogos de futebol do dia...")
    try:
        today = datetime.now(BRASILIA).strftime("%Y-%m-%d")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        # API gratuita de futebol
        url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{today}"
        r = requests.get(url, headers=headers, timeout=15)
        data = r.json()

        eventos = data.get("events", [])
        log(f"  {len(eventos)} jogos encontrados")

        # Filtra ligas prioritárias
        ligas_foco = [
            "Premier League", "La Liga", "Serie A", "Bundesliga",
            "Ligue 1", "Champions League", "Europa League",
            "Brasileirao", "Serie B", "Copa do Brasil",
            "NBA"
        ]

        supabase.table("jogos_hoje").delete().eq("sport", "futebol").execute()

        count = 0
        for ev in eventos:
            liga = ev.get("tournament", {}).get("name", "")
            if not any(l.lower() in liga.lower() for l in ligas_foco):
                continue

            time_casa = ev.get("homeTeam", {}).get("name", "")
            time_fora = ev.get("awayTeam", {}).get("name", "")
            start_ts  = ev.get("startTimestamp", 0)
            ev_id     = ev.get("id", "")

            # Converte horário pra Brasília
            if start_ts:
                dt_utc = datetime.utcfromtimestamp(start_ts).replace(tzinfo=pytz.utc)
                dt_br  = dt_utc.astimezone(BRASILIA)
                horario_br = dt_br.strftime("%H:%M")
            else:
                horario_br = "?"

            status_code = ev.get("status", {}).get("type", "notstarted")

            jogo = {
                "sport": "futebol",
                "liga": liga,
                "time_casa": time_casa,
                "time_fora": time_fora,
                "horario_brasilia": horario_br,
                "status": status_code,
                "ausencias": json.dumps([]),
                "odds": json.dumps({}),
                "updated_at": datetime.now(BRASILIA).isoformat(),
            }
            supabase.table("jogos_hoje").insert(jogo).execute()
            count += 1

        log(f"  {count} jogos de ligas foco salvos")
        fetch_escalacoes_futebol()

    except Exception as e:
        log(f"Erro futebol: {e}")

def fetch_escalacoes_futebol():
    """Busca escalações dos jogos salvos que estão próximos (próximas 3h)"""
    log("Verificando escalações...")
    agora = datetime.now(BRASILIA)
    try:
        jogos = supabase.table("jogos_hoje").select("*").eq("sport", "futebol").eq("escalacao_confirmada", False).execute()

        for jogo in jogos.data:
            horario_str = jogo.get("horario_brasilia", "")
            if not horario_str or horario_str == "?":
                continue

            try:
                hh, mm = horario_str.split(":")
                dt_jogo = agora.replace(hour=int(hh), minute=int(mm), second=0)
                diff_min = (dt_jogo - agora).total_seconds() / 60

                # Se o jogo é nas próximas 3 horas, tenta buscar escalação
                if 0 < diff_min < 180:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{agora.strftime('%Y-%m-%d')}"
                    # Marca como pendente de escalação
                    supabase.table("jogos_hoje").update({
                        "status": "aguardando_escalacao",
                        "updated_at": agora.isoformat()
                    }).eq("id", jogo["id"]).execute()

                    if diff_min < 75:
                        # Cria alerta de escalação pendente
                        existing = supabase.table("alertas").select("id").eq("jogo_id", jogo["id"]).eq("tipo", "escalacao_pendente").execute()
                        if not existing.data:
                            supabase.table("alertas").insert({
                                "tipo": "escalacao_pendente",
                                "titulo": f"⏳ ESCALAÇÃO PENDENTE — {jogo['time_casa']} vs {jogo['time_fora']}",
                                "descricao": f"Jogo em {int(diff_min)}min. Confirme escalação antes de entrar.",
                                "jogo_id": jogo["id"],
                                "sport": "futebol",
                                "prioridade": "alta",
                                "fonte": "BetEdge",
                            }).execute()
                            log(f"  Alerta escalação: {jogo['time_casa']} vs {jogo['time_fora']}")
            except Exception as inner:
                continue

    except Exception as e:
        log(f"Erro escalações: {e}")

# ─────────────────────────────────────────────
# MATCHUPS COM IA
# ─────────────────────────────────────────────
def gerar_matchups_ia():
    log("Gerando análise de matchups com IA...")
    try:
        jogos = supabase.table("jogos_hoje").select("*").execute()
        if not jogos.data:
            return

        resumo = []
        for j in jogos.data[:8]:  # Máx 8 jogos por vez
            resumo.append(f"{j['sport'].upper()} | {j['liga']} | {j['time_fora']} @ {j['time_casa']} | {j['horario_brasilia']}")

        prompt = f"""Hoje é {datetime.now(BRASILIA).strftime('%d/%m/%Y')}. Horário Brasília.

Jogos do dia:
{chr(10).join(resumo)}

Analise os 3 melhores matchups para apostas de valor alto (odds 2.0+).
Para cada um:
1. Mercado específico (não apenas resultado)
2. Fundamento estatístico
3. Odd esperada
4. Nível de confiança (1-10)
5. Stake sugerida (% da banca)

Foque em: escanteios 1T, finalizações, desarmes, props de jogadores NBA (pts/reb/ast).
Seja direto. Sem jogo sem fundamento = sem entrada."""

        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60
        )
        data = r.json()
        analise = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "Sem análise disponível")

        # Salva como alerta de matchup
        supabase.table("alertas").insert({
            "tipo": "matchup",
            "titulo": f"🎯 MATCHUPS DO DIA — {datetime.now(BRASILIA).strftime('%d/%m')}",
            "descricao": analise,
            "sport": "geral",
            "prioridade": "normal",
            "fonte": "BetEdge IA",
        }).execute()
        log("  Matchups IA gerados")

    except Exception as e:
        log(f"Erro matchups IA: {e}")

# ─────────────────────────────────────────────
# UPDATE CONFIG
# ─────────────────────────────────────────────
def update_ultima_atualizacao():
    agora = datetime.now(BRASILIA).isoformat()
    supabase.table("configuracoes").update({"valor": agora}).eq("chave", "ultima_atualizacao").execute()

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
def rotina_completa():
    log("=== ROTINA COMPLETA ===")
    fetch_nba_jogos()
    fetch_futebol_jogos()
    gerar_matchups_ia()
    update_ultima_atualizacao()
    log("=== FIM DA ROTINA ===")

def rotina_escalacoes():
    fetch_escalacoes_futebol()
    fetch_nba_jogos()  # Atualiza lesões NBA também
    update_ultima_atualizacao()

if __name__ == "__main__":
    log("BetEdge Backend iniciado")
    log(f"Fuso horário: {BRASILIA}")

    # Roda imediatamente na inicialização
    rotina_completa()

    # Agenda horários fixos (Brasília)
    schedule.every().day.at("07:00").do(rotina_completa)   # Manhã — jogos do dia
    schedule.every().day.at("10:00").do(rotina_completa)   # Atualiza matchups
    schedule.every().day.at("13:00").do(rotina_escalacoes) # Early escalações
    schedule.every().day.at("15:00").do(rotina_escalacoes) # Escalações tarde
    schedule.every().day.at("17:00").do(rotina_escalacoes) # Pré-jogo Europa
    schedule.every().day.at("19:00").do(rotina_escalacoes) # Pré-jogo noite
    schedule.every().day.at("21:00").do(rotina_escalacoes) # NBA começa
    schedule.every(30).minutes.do(rotina_escalacoes)       # A cada 30min sempre

    log("Scheduler ativo. Próximas atualizações a cada 30 min + horários fixos.")

    while True:
        schedule.run_pending()
        time.sleep(60)
