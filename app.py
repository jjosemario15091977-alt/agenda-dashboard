import hashlib
import hmac
import time as time_module
from datetime import datetime, timedelta, time
from typing import Literal, Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import BaseModel, Field
from streamlit_calendar import calendar
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai
from google.genai import types

# --- CONFIGURAÇÃO INICIAL ---
st.set_page_config(page_title="Meus agendamentos", page_icon="🗓️", layout="wide")


# --- CONTROLE DE AUTENTICAÇÃO ---
def verificar_autenticacao() -> bool:
    """Exibe a tela de login até que usuário e senha válidos sejam informados."""
    if st.session_state.get("autenticado", False):
        return True

    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 4rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col_central, _ = st.columns([1.2, 1.4, 1.2])
    with col_central:
        with st.container(border=True):
            st.markdown("### 🔒 Acesso Restrito")
            st.caption("Informe suas credenciais para acessar **Meus agendamentos**.")

            with st.form("form_login", clear_on_submit=False):
                usuario_digitado = st.text_input("Usuário", placeholder="Digite seu usuário")
                senha_digitada = st.text_input("Senha", type="password", placeholder="Digite sua senha")
                entrar = st.form_submit_button("Entrar", type="primary", use_container_width=True)

                if entrar:
                    usuario_correto = str(st.secrets.get("APP_USUARIO", "agenda"))
                    senha_correta = str(st.secrets.get("APP_SENHA", "123456"))

                    u_ok = hmac.compare_digest(usuario_digitado.strip(), usuario_correto)
                    s_ok = hmac.compare_digest(senha_digitada, senha_correta)

                    if u_ok and s_ok:
                        st.session_state["autenticado"] = True
                        st.rerun()
                    else:
                        st.error("Usuário ou senha incorretos.")

    return False


if not verificar_autenticacao():
    st.stop()


# --- ESTILOS DO DASHBOARD ---
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
    }
    div[data-testid="stMetric"] {
        background-color: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.1);
        padding: 1rem 1.25rem;
        border-radius: 0.75rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

TIMEZONE = "America/Bahia"
TZ = ZoneInfo(TIMEZONE)
CIDADE_PADRAO = "Feira de Santana, Bahia, Brasil"

CALENDARS = {
    "Pessoal": {
        "id": st.secrets["CALENDAR_ID_PESSOAL"],
        "color": "#16A34A",  # Verde
    },
    "Profissional": {
        "id": st.secrets["CALENDAR_ID_PROFISSIONAL"],
        "color": "#2563EB",  # Azul
    },
}

LEMBRETES_OPCOES = {
    "Sem lembrete": None,
    "10 minutos antes (Pop-up)": [{"method": "popup", "minutes": 10}],
    "30 minutos antes (Pop-up)": [{"method": "popup", "minutes": 30}],
    "1 hora antes (Pop-up + E-mail)": [
        {"method": "popup", "minutes": 60},
        {"method": "email", "minutes": 60},
    ],
    "1 dia antes (Pop-up + E-mail)": [
        {"method": "popup", "minutes": 1440},
        {"method": "email", "minutes": 1440},
    ],
}

# Inicializa estados da sessão
if "ultimo_evento_criado" not in st.session_state:
    st.session_state.ultimo_evento_criado = None
if "ultimo_audio_hash" not in st.session_state:
    st.session_state.ultimo_audio_hash = None
if "data_foco_calendario" not in st.session_state:
    st.session_state.data_foco_calendario = datetime.now(TZ).strftime("%Y-%m-%d")
if "cal_reset_counter" not in st.session_state:
    st.session_state.cal_reset_counter = 0


# --- SCHEMA ESTRUTURADO PARA O GEMINI ---
class EventoExtraido(BaseModel):
    titulo: str = Field(description="Título curto e claro do compromisso ou tarefa")
    categoria: Literal["Profissional", "Pessoal"] = Field(
        description="Classifique como 'Profissional' (aulas, reuniões, escola, projetos, código, trabalho) ou 'Pessoal' (família, médicos, casa, lazer, compras, serviços públicos)."
    )
    data_inicio_iso: str = Field(
        description="Data e hora de início no formato ISO 8601 completo com fuso horário -03:00 (ex: 2026-11-16T14:00:00-03:00)."
    )
    data_fim_iso: str = Field(
        description="Data e hora de término no formato ISO 8601 completo com fuso horário -03:00. Se não mencionada, adicione 1 hora ao início."
    )
    lembrete_minutos: int = Field(
        default=30,
        description="Minutos de antecedência para o lembrete. Use 30 como padrão se o usuário não especificar.",
    )
    localizacao: str = Field(
        description=(
            "OBRIGATÓRIO quando houver qualquer local físico (ex: SAC, hospital, clínica, escola, cartório, banco, shopping, loja). "
            "Retorne o nome do estabelecimento seguido do endereço completo (rua/avenida, bairro, cidade - UF) para GPS. "
            "Exemplo para SAC em Feira de Santana: 'SAC Feira II - Rua Vasco Filho, Centro, Feira de Santana - BA'. "
            "Retorne string vazia '' APENAS se for uma tarefa puramente online ou em casa."
        )
    )
    descricao: str = Field(
        default="",
        description="Detalhes adicionais mencionados pelo usuário, se houver.",
    )


# --- CLIENTES DE API ---
def get_calendar_service():
    """Cria uma instância limpa do serviço para evitar erro 10054 de socket expirado."""
    creds_dict = dict(st.secrets["gcp_service_account"])
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


@st.cache_resource
def get_gemini_client():
    return genai.Client(
        api_key=st.secrets["GEMINI_API_KEY"],
        http_options={"api_version": "v1beta"},
    )


# --- FUNÇÕES DE AGENDA ---
@st.cache_data(ttl=120)
def carregar_eventos():
    service = get_calendar_service()
    agora = datetime.now(TZ)
    time_min = (agora - timedelta(days=60)).isoformat()
    time_max = (agora + timedelta(days=180)).isoformat()

    todos_eventos = []

    for categoria, info in CALENDARS.items():
        try:
            events_result = (
                service.events()
                .list(
                    calendarId=info["id"],
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute(num_retries=3)
            )
            for item in events_result.get("items", []):
                start = item["start"].get("dateTime", item["start"].get("date"))
                end = item["end"].get("dateTime", item["end"].get("date"))
                todos_eventos.append(
                    {
                        "id": item["id"],
                        "title": item.get("summary", "(Sem título)"),
                        "start": start,
                        "end": end,
                        "color": info["color"],
                        "extendedProps": {
                            "categoria": categoria,
                            "calendarId": info["id"],
                            "descricao": item.get("description", ""),
                            "localizacao": item.get("location", ""),
                        },
                    }
                )
        except Exception as e:
            st.error(f"Erro ao carregar agenda '{categoria}': {e}")

    return todos_eventos


def criar_evento(
    categoria: str,
    titulo: str,
    descricao: str,
    dt_inicio: datetime,
    dt_fim: datetime,
    overrides: Optional[list],
    localizacao: str = "",
):
    service = get_calendar_service()
    calendar_id = CALENDARS[categoria]["id"]

    body = {
        "summary": titulo,
        "description": descricao or "",
        "location": localizacao or "",
        "start": {"dateTime": dt_inicio.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": dt_fim.isoformat(), "timeZone": TIMEZONE},
        "reminders": (
            {"useDefault": False, "overrides": overrides}
            if overrides
            else {"useDefault": False, "overrides": []}
        ),
    }

    criado = (
        service.events()
        .insert(calendarId=calendar_id, body=body)
        .execute(num_retries=3)
    )
    carregar_eventos.clear()

    # Move a visualização do calendário diretamente para o mês/dia do novo evento
    st.session_state.data_foco_calendario = dt_inicio.strftime("%Y-%m-%d")

    # Guarda referência para o botão "Desfazer"
    st.session_state.ultimo_evento_criado = {
        "event_id": criado["id"],
        "calendar_id": calendar_id,
        "titulo": titulo,
        "categoria": categoria,
        "inicio_fmt": dt_inicio.strftime("%d/%m às %H:%M"),
        "localizacao": localizacao or "",
    }


def excluir_evento(calendar_id: str, event_id: str):
    service = get_calendar_service()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute(num_retries=3)
    carregar_eventos.clear()


# --- INTERPRETAÇÃO INTELIGENTE COM GEMINI (COM FALLBACK AUTOMÁTICO) ---
def processar_entrada_ia(texto: Optional[str] = None, audio_bytes: Optional[bytes] = None) -> EventoExtraido:
    client = get_gemini_client()
    agora = datetime.now(TZ)
    dias_semana = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
    dia_semana_str = dias_semana[agora.weekday()]

    prompt_sistema = (
        f"Você é um assistente de agenda pessoal e profissional localizado em {CIDADE_PADRAO}. "
        f"Data e hora atual de referência: {agora.strftime('%Y-%m-%d %H:%M')} ({dia_semana_str}), fuso {TIMEZONE} (-03:00). "
        f"Extraia os dados do evento solicitado pelo usuário (por texto ou áudio). "
        f"Resolva expressões relativas como 'hoje', 'amanhã', 'sexta que vem' ou 'daqui a 2 horas' com base na data de referência. "
        f"REGRA DE LOCALIZAÇÃO: Sempre que o usuário citar um local físico (ex.: SAC, hospital, clínica, escola, cartório, shopping, banco), "
        f"você DEVE preencher o campo 'localizacao' com o nome do local e o endereço completo (rua, bairro, Feira de Santana - BA) "
        f"mesmo que o usuário não tenha dito a rua."
    )

    conteudos = [prompt_sistema]
    if texto:
        conteudos.append(f"Comando do usuário: {texto}")
    if audio_bytes:
        conteudos.append(
            types.Part.from_bytes(
                data=audio_bytes,
                mime_type="audio/wav",
            )
        )

    modelos_fallback = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    ultimo_erro = None

    for modelo in modelos_fallback:
        for _ in range(2):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=conteudos,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=EventoExtraido,
                        temperature=0.1,
                    ),
                )
                return EventoExtraido.model_validate_json(response.text)
            except Exception as e:
                ultimo_erro = e
                erro_str = str(e)
                if "503" in erro_str or "UNAVAILABLE" in erro_str or "429" in erro_str:
                    time_module.sleep(1)
                    continue
                raise e

    raise ultimo_erro


def salvar_evento_extraido(ev: EventoExtraido):
    dt_ini = datetime.fromisoformat(ev.data_inicio_iso)
    dt_fim = datetime.fromisoformat(ev.data_fim_iso)
    if dt_fim <= dt_ini:
        dt_fim = dt_ini + timedelta(hours=1)

    overrides = [{"method": "popup", "minutes": ev.lembrete_minutos}] if ev.lembrete_minutos > 0 else None

    criar_evento(
        categoria=ev.categoria,
        titulo=ev.titulo,
        descricao=ev.descricao or "",
        dt_inicio=dt_ini,
        dt_fim=dt_fim,
        overrides=overrides,
        localizacao=(ev.localizacao or "").strip(),
    )


def formatar_data_hora_exibicao(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        if "T" in iso_str:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%d/%m/%Y às %H:%M")
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d/%m/%Y (Dia inteiro)")
    except Exception:
        return iso_str


def gerar_link_rota_maps(destino: str) -> str:
    destino_codificado = quote_plus(destino.strip())
    return f"https://www.google.com/maps/dir/?api=1&destination={destino_codificado}&travelmode=driving"


# --- JANELA MODAL AMPLIADA DE DETALHES DO EVENTO ---
@st.dialog("Detalhes do Compromisso", width="large")
def abrir_modal_evento(ev_clicado: dict):
    props = ev_clicado.get("extendedProps", {})
    titulo_ev = ev_clicado.get("title", "")
    categoria_ev = props.get("categoria", "")
    inicio_fmt = formatar_data_hora_exibicao(ev_clicado.get("start", ""))
    fim_fmt = formatar_data_hora_exibicao(ev_clicado.get("end", ""))
    local_txt = props.get("localizacao", "").strip()
    descricao_txt = props.get("descricao", "").strip()

    # Fallback para eventos criados anteriormente sem o campo location preenchido
    destino_maps = local_txt if local_txt else f"{titulo_ev}, Feira de Santana - BA"

    cor_badge = "🔵" if categoria_ev == "Profissional" else "🟢"
    st.subheader(titulo_ev)
    st.caption(f"{cor_badge} Agenda **{categoria_ev}**")

    st.markdown("---")
    c_ini, c_fim = st.columns(2)
    with c_ini:
        with st.container(border=True):
            st.markdown(f"**🕒 Início**\n\n{inicio_fmt}")
    with c_fim:
        with st.container(border=True):
            st.markdown(f"**🏁 Término**\n\n{fim_fmt}")

    if local_txt:
        with st.container(border=True):
            st.markdown(f"**📍 Endereço / Localização**\n\n{local_txt}")

    with st.container(border=True):
        st.markdown("**📝 Observações / Detalhes**")
        if descricao_txt:
            st.write(descricao_txt)
        else:
            st.caption("Nenhuma observação adicional registrada neste evento.")

    st.write("")
    col_maps, col_del = st.columns(2)
    with col_maps:
        st.link_button(
            "🗺️ Abrir Rota no Google Maps",
            url=gerar_link_rota_maps(destino_maps),
            type="primary",
            use_container_width=True,
        )
    with col_del:
        if st.button("🗑️ Excluir Evento", type="secondary", use_container_width=True):
            try:
                excluir_evento(props["calendarId"], ev_clicado["id"])
                st.toast("Evento removido do Google Agenda!")
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao excluir: {e}")


# --- BARRA LATERAL ---
with st.sidebar:
    st.header("🎙️ Criação Rápida com IA")
    st.caption("Fale ou digite naturalmente. O evento é salvo automaticamente na agenda certa.")

    # 1. Entrada por Voz (Zero-Click Save)
    audio_gravado = st.audio_input("Falar compromisso", key="mic_input")
    if audio_gravado is not None:
        raw_bytes = audio_gravado.getvalue()
        audio_hash = hashlib.md5(raw_bytes).hexdigest()

        if audio_hash != st.session_state.ultimo_audio_hash:
            st.session_state.ultimo_audio_hash = audio_hash
            with st.spinner("Ouvindo e agendando..."):
                try:
                    ev_extraido = processar_entrada_ia(audio_bytes=raw_bytes)
                    salvar_evento_extraido(ev_extraido)
                    st.rerun()
                except Exception as e:
                    st.error(f"Não consegui processar o áudio: {e}")

    # 2. Entrada Rápida por Texto
    with st.form("form_texto_rapido", clear_on_submit=True):
        texto_rapido = st.text_input(
            "Ou digite em linguagem natural:",
            placeholder="Ex.: Agendamento no SAC amanhã às 10h",
        )
        if st.form_submit_button("⚡ Agendar Texto", use_container_width=True):
            if texto_rapido.strip():
                with st.spinner("Interpretando e agendando..."):
                    try:
                        ev_extraido = processar_entrada_ia(texto=texto_rapido.strip())
                        salvar_evento_extraido(ev_extraido)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erro ao interpretar comando: {e}")

    # Banner de confirmação + Botão Desfazer Último Evento
    if st.session_state.ultimo_evento_criado:
        ult = st.session_state.ultimo_evento_criado
        msg_sucesso = f"✅ **{ult['titulo']}** ({ult['categoria']}) — {ult['inicio_fmt']}"
        if ult.get("localizacao"):
            msg_sucesso += f"\n\n📍 {ult['localizacao']}"
        st.success(msg_sucesso)

        if st.button("↩️ Desfazer Último Evento", type="secondary", use_container_width=True):
            try:
                excluir_evento(ult["calendar_id"], ult["event_id"])
                st.session_state.ultimo_evento_criado = None
                st.toast("Último evento removido!")
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao desfazer: {e}")

    st.markdown("---")
    st.subheader("🎛️ Filtros de Visualização")
    filtro_cat = st.radio(
        "Exibir:",
        ["Todas", "Profissional", "Pessoal"],
        horizontal=True,
    )

    if st.button("🔄 Sincronizar Google Agenda", use_container_width=True):
        carregar_eventos.clear()
        st.session_state.data_foco_calendario = datetime.now(TZ).strftime("%Y-%m-%d")
        st.session_state.cal_reset_counter += 1
        st.rerun()

    # 3. Formulário Manual Completo
    with st.expander("📝 Cadastro Manual Detalhado"):
        with st.form("form_manual", clear_on_submit=True):
            titulo_m = st.text_input("Título *")
            categoria_m = st.selectbox("Categoria", ["Profissional", "Pessoal"])
            local_m = st.text_input("Local / Endereço (opcional)", placeholder="Ex.: SAC Feira II, Centro")
            data_m = st.date_input("Data", value=datetime.now(TZ).date())

            c1, c2 = st.columns(2)
            with c1:
                hora_ini_m = st.time_input("Início", value=time(9, 0))
            with c2:
                hora_fim_m = st.time_input("Término", value=time(10, 0))

            lembrete_m = st.selectbox("Lembrete", list(LEMBRETES_OPCOES.keys()), index=2)
            desc_m = st.text_area("Observações", height=60)

            if st.form_submit_button("Salvar Manualmente", use_container_width=True):
                if titulo_m.strip():
                    dt_ini = datetime.combine(data_m, hora_ini_m, tzinfo=TZ)
                    dt_fim = datetime.combine(data_m, hora_fim_m, tzinfo=TZ)
                    if dt_fim <= dt_ini:
                        dt_fim = dt_ini + timedelta(hours=1)
                    criar_evento(
                        categoria=categoria_m,
                        titulo=titulo_m.strip(),
                        descricao=desc_m.strip(),
                        dt_inicio=dt_ini,
                        dt_fim=dt_fim,
                        overrides=LEMBRETES_OPCOES[lembrete_m],
                        localizacao=local_m.strip(),
                    )
                    st.rerun()

    st.markdown("---")
    if st.button("🚪 Sair", use_container_width=True):
        st.session_state["autenticado"] = False
        st.rerun()


# --- ÁREA PRINCIPAL: KPIS E CALENDÁRIO ---
st.title("Meus agendamentos")

eventos = carregar_eventos()
hoje_str = datetime.now(TZ).strftime("%Y-%m-%d")

eventos_hoje = [e for e in eventos if e["start"].startswith(hoje_str)]
prof_count = sum(1 for e in eventos if e["extendedProps"]["categoria"] == "Profissional")
pess_count = sum(1 for e in eventos if e["extendedProps"]["categoria"] == "Pessoal")

k1, k2, k3 = st.columns(3)
k1.metric("Compromissos Hoje", len(eventos_hoje))
k2.metric("🔵 Profissional (Período)", prof_count)
k3.metric("🟢 Pessoal (Período)", pess_count)

st.write("")

if filtro_cat != "Todas":
    eventos_filtrados = [e for e in eventos if e["extendedProps"]["categoria"] == filtro_cat]
else:
    eventos_filtrados = eventos

calendar_options = {
    "editable": False,
    "selectable": True,
    "initialDate": st.session_state.data_foco_calendario,
    "headerToolbar": {
        "left": "today prev,next",
        "center": "title",
        "right": "dayGridMonth,listMonth",
    },
    "buttonText": {
        "today": "Hoje",
        "month": "Mês",
        "list": "Lista",
    },
    "eventTimeFormat": {
        "hour": "2-digit",
        "minute": "2-digit",
        "hour12": False,
    },
    "initialView": "dayGridMonth",
    "locale": "pt-br",
    "height": 680,
}

estado_calendario = calendar(
    events=eventos_filtrados,
    options=calendar_options,
    callbacks=["eventClick"],
    key=f"cal_{filtro_cat}_{st.session_state.data_foco_calendario}_{st.session_state.cal_reset_counter}",
)

# --- ABERTURA DO POP-UP AO CLICAR NO EVENTO (MÊS OU LISTA) ---
if estado_calendario and estado_calendario.get("eventClick"):
    ev_clicado = estado_calendario["eventClick"]["event"]
    st.session_state.cal_reset_counter += 1
    abrir_modal_evento(ev_clicado)