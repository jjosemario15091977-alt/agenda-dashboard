import base64
import hashlib
import hmac
import io
import json
import secrets as py_secrets
import time as time_module
from datetime import datetime, timedelta, time
from typing import Literal, Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import streamlit as st
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from streamlit_calendar import calendar
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai
from google.genai import types

# --- CONFIGURAÇÃO INICIAL ---
st.set_page_config(
    page_title="My Diaries",
    layout="wide",
    initial_sidebar_state="auto",
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

# Constantes do cofre de credenciais e perfil no Google Calendar (ano 2099)
VAULT_EVENT_TAG = "APP_AUTH_VAULT_V1"
VAULT_TIME_MIN = "2099-01-01T00:00:00Z"
VAULT_TIME_MAX = "2099-01-02T00:00:00Z"

# Inicializa estados da sessão
if "ultimo_evento_criado" not in st.session_state:
    st.session_state.ultimo_evento_criado = None
if "ultimo_audio_hash" not in st.session_state:
    st.session_state.ultimo_audio_hash = None
if "data_foco_calendario" not in st.session_state:
    st.session_state.data_foco_calendario = datetime.now(TZ).strftime("%Y-%m-%d")
if "cal_reset_counter" not in st.session_state:
    st.session_state.cal_reset_counter = 0
if "modo_recuperacao_senha" not in st.session_state:
    st.session_state.modo_recuperacao_senha = False


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


# --- COFRE DE SENHA E PERFIL PERSISTENTE NO GOOGLE CALENDAR ---
def _gerar_hash_senha(senha: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return dk.hex()


def _obter_evento_cofre():
    """Busca o registro oculto de senha e perfil na agenda pessoal."""
    service = get_calendar_service()
    cal_id = CALENDARS["Pessoal"]["id"]
    res = (
        service.events()
        .list(
            calendarId=cal_id,
            timeMin=VAULT_TIME_MIN,
            timeMax=VAULT_TIME_MAX,
            privateExtendedProperty=f"vault_tag={VAULT_EVENT_TAG}",
            singleEvents=True,
        )
        .execute(num_retries=3)
    )
    items = res.get("items", [])
    return (cal_id, items[0]) if items else (cal_id, None)


def _ler_dados_cofre() -> dict:
    """Lê os dados de perfil (nome e foto Base64) armazenados na descrição do cofre."""
    try:
        _, evento_existente = _obter_evento_cofre()
        if evento_existente:
            desc = evento_existente.get("description", "")
            if desc.startswith("{"):
                return json.loads(desc)
    except Exception:
        pass
    return {}


@st.cache_data(ttl=300)
def carregar_perfil_usuario() -> dict:
    dados = _ler_dados_cofre()
    return {
        "nome": dados.get("nome_exibicao", ""),
        "avatar_b64": dados.get("avatar_b64", ""),
    }


def _gravar_evento_cofre(
    pwd_salt: Optional[str] = None,
    pwd_hash: Optional[str] = None,
    nome_exibicao: Optional[str] = None,
    avatar_b64: Optional[str] = None,
):
    """Atualiza senha e/ou dados de perfil preservando os campos já salvos no cofre."""
    service = get_calendar_service()
    cal_id, evento_existente = _obter_evento_cofre()

    priv_atual = {}
    dados_desc_atual = {}
    if evento_existente:
        priv_atual = dict(evento_existente.get("extendedProperties", {}).get("private", {}))
        desc_raw = evento_existente.get("description", "")
        if desc_raw.startswith("{"):
            try:
                dados_desc_atual = json.loads(desc_raw)
            except Exception:
                dados_desc_atual = {}

    priv_atual["vault_tag"] = VAULT_EVENT_TAG
    if pwd_salt is not None and pwd_hash is not None:
        priv_atual["pwd_salt"] = pwd_salt
        priv_atual["pwd_hash"] = pwd_hash

    if nome_exibicao is not None:
        dados_desc_atual["nome_exibicao"] = nome_exibicao
    if avatar_b64 is not None:
        dados_desc_atual["avatar_b64"] = avatar_b64

    corpo = {
        "summary": "[Sistema] Registro de Segurança e Perfil",
        "description": json.dumps(dados_desc_atual, ensure_ascii=False),
        "visibility": "private",
        "transparency": "transparent",
        "start": {"dateTime": "2099-01-01T00:00:00-03:00", "timeZone": TIMEZONE},
        "end": {"dateTime": "2099-01-01T00:05:00-03:00", "timeZone": TIMEZONE},
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": priv_atual},
    }

    if evento_existente:
        service.events().update(
            calendarId=cal_id, eventId=evento_existente["id"], body=corpo
        ).execute(num_retries=3)
    else:
        service.events().insert(calendarId=cal_id, body=corpo).execute(num_retries=3)

    carregar_perfil_usuario.clear()


def salvar_nova_senha_cofre(nova_senha: str):
    salt = py_secrets.token_hex(16)
    pwd_hash = _gerar_hash_senha(nova_senha, salt)
    _gravar_evento_cofre(pwd_salt=salt, pwd_hash=pwd_hash)


def processar_imagem_avatar(arquivo_upload) -> str:
    """Redimensiona e comprime a foto enviada priorizando o enquadramento do rosto."""
    img = Image.open(arquivo_upload)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, (220, 220), method=Image.Resampling.LANCZOS, centering=(0.5, 0.25))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def validar_senha_usuario(senha_digitada: str) -> bool:
    """Valida a senha contra o cofre persistente; se não houver alteração salva, usa st.secrets."""
    try:
        _, evento_cofre = _obter_evento_cofre()
        if evento_cofre:
            priv = evento_cofre.get("extendedProperties", {}).get("private", {})
            salt = priv.get("pwd_salt", "")
            hash_salvo = priv.get("pwd_hash", "")
            if salt and hash_salvo:
                hash_tentativa = _gerar_hash_senha(senha_digitada, salt)
                return hmac.compare_digest(hash_tentativa, hash_salvo)
    except Exception:
        pass

    senha_inicial = str(st.secrets.get("APP_SENHA", "123456"))
    return hmac.compare_digest(senha_digitada, senha_inicial)


# --- CONTROLE DE AUTENTICAÇÃO E RECUPERAÇÃO ---
def verificar_autenticacao() -> bool:
    """Exibe a tela de login e o fluxo de redefinição de senha por PIN Mestre."""
    if st.session_state.get("autenticado", False):
        return True

    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 3.5rem;
            padding-left: 1rem;
            padding-right: 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col_central, _ = st.columns([1, 1.5, 1])
    with col_central:
        with st.container(border=True):
            if not st.session_state.modo_recuperacao_senha:
                st.markdown("### Acesso Restrito")
                st.caption("Informe suas credenciais para acessar **My Diaries**.")

                with st.form("form_login", clear_on_submit=False):
                    usuario_digitado = st.text_input("Usuário", placeholder="Digite seu usuário")
                    senha_digitada = st.text_input("Senha", type="password", placeholder="Digite sua senha")
                    entrar = st.form_submit_button("Entrar", type="primary", use_container_width=True)

                    if entrar:
                        usuario_correto = str(st.secrets.get("APP_USUARIO", "agenda"))
                        u_ok = hmac.compare_digest(usuario_digitado.strip(), usuario_correto)
                        s_ok = validar_senha_usuario(senha_digitada)

                        if u_ok and s_ok:
                            st.session_state["autenticado"] = True
                            st.rerun()
                        else:
                            st.error("Usuário ou senha incorretos.")

                st.write("")
                if st.button("Esqueci minha senha", use_container_width=True):
                    st.session_state.modo_recuperacao_senha = True
                    st.rerun()

            else:
                st.markdown("### Redefinir Senha")
                st.caption("Informe seu usuário e o **PIN Mestre de Recuperação** para cadastrar uma nova senha.")

                with st.form("form_recuperar_senha", clear_on_submit=True):
                    usuario_rec = st.text_input("Usuário", placeholder="Ex.: agenda")
                    pin_digitado = st.text_input(
                        "PIN Mestre de Recuperação",
                        type="password",
                        placeholder="Digite o PIN configurado",
                    )
                    nova_senha_rec = st.text_input("Nova senha (mín. 4 caracteres)", type="password")
                    confirmar_senha_rec = st.text_input("Confirmar nova senha", type="password")
                    salvar_rec = st.form_submit_button("Salvar Nova Senha", type="primary", use_container_width=True)

                    if salvar_rec:
                        usuario_correto = str(st.secrets.get("APP_USUARIO", "agenda"))
                        pin_correto = str(st.secrets.get("RECOVERY_PIN", "2026"))

                        u_ok = hmac.compare_digest(usuario_rec.strip(), usuario_correto)
                        pin_ok = hmac.compare_digest(pin_digitado.strip(), pin_correto)

                        if not u_ok or not pin_ok:
                            st.error("Usuário ou PIN Mestre inválido.")
                        elif len(nova_senha_rec) < 4:
                            st.warning("A nova senha deve ter pelo menos 4 caracteres.")
                        elif nova_senha_rec != confirmar_senha_rec:
                            st.warning("A confirmação de senha não confere.")
                        else:
                            try:
                                with st.spinner("Atualizando senha..."):
                                    salvar_nova_senha_cofre(nova_senha_rec)
                                st.success("Senha redefinida com sucesso! Faça login com a nova senha.")
                                st.session_state.modo_recuperacao_senha = False
                                time_module.sleep(1)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Não foi possível salvar a nova senha: {e}")

                st.write("")
                if st.button("Voltar para o Login", use_container_width=True):
                    st.session_state.modo_recuperacao_senha = False
                    st.rerun()

    return False


if not verificar_autenticacao():
    st.stop()


# --- ESTILOS RESPONSIVOS + CABEÇALHO FIXO (STICKY) ---
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 3.2rem !important;
        padding-bottom: 2rem !important;
    }
    /* Fixa o cabeçalho superior (perfil, título e indicadores) ao rolar a página */
    div[data-testid="stVerticalBlock"]:has(> div.element-container #sticky-header-anchor) {
        position: sticky;
        top: 2.85rem;
        z-index: 90;
        background-color: var(--background-color);
        padding-top: 0.45rem;
        padding-bottom: 0.75rem;
        border-bottom: 1px solid rgba(128, 128, 128, 0.18);
        margin-bottom: 0.5rem;
    }
    div[data-testid="stMetric"] {
        background-color: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.1);
        padding: 0.85rem 1.05rem;
        border-radius: 0.75rem;
    }
    .perfil-header {
        display: flex;
        align-items: center;
        gap: 1.1rem;
        margin-bottom: 0.35rem;
    }
    .perfil-avatar {
        width: 76px;
        height: 76px;
        min-width: 76px;
        border-radius: 50%;
        object-fit: cover;
        object-position: center 20%;
        border: 3px solid #2563EB;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        display: block;
    }
    .perfil-avatar-placeholder {
        width: 76px;
        height: 76px;
        min-width: 76px;
        border-radius: 50%;
        background: linear-gradient(135deg, #2563EB, #16A34A);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        font-size: 1.85rem;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }
    .perfil-texto {
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .perfil-saudacao {
        font-size: 1.05rem;
        opacity: 0.82;
        margin: 0 0 2px 0;
        line-height: 1.2;
    }
    .perfil-nome {
        font-size: 1.65rem;
        font-weight: 700;
        margin: 0;
        line-height: 1.2;
    }
    /* Ajustes específicos para telas de celular (largura até 768px) */
    @media (max-width: 768px) {
        .block-container {
            padding-top: 2.9rem !important;
            padding-left: 0.65rem !important;
            padding-right: 0.65rem !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div.element-container #sticky-header-anchor) {
            top: 2.65rem;
            padding-top: 0.35rem;
            padding-bottom: 0.55rem;
        }
        h1 {
            font-size: 1.45rem !important;
            margin-bottom: 0.35rem !important;
        }
        .perfil-header {
            gap: 0.8rem;
            margin-bottom: 0.25rem;
        }
        .perfil-avatar, .perfil-avatar-placeholder {
            width: 56px;
            height: 56px;
            min-width: 56px;
            font-size: 1.35rem;
        }
        .perfil-saudacao {
            font-size: 0.9rem;
        }
        .perfil-nome {
            font-size: 1.25rem;
        }
        div[data-testid="stMetric"] {
            padding: 0.55rem 0.75rem !important;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.2rem !important;
        }
        div[data-testid="stMetricLabel"] {
            font-size: 0.75rem !important;
        }
        iframe[title="streamlit_calendar.calendar"] {
            width: 100% !important;
            min-height: 460px !important;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
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
                priv_props = item.get("extendedProperties", {}).get("private", {})
                if priv_props.get("vault_tag") == VAULT_EVENT_TAG:
                    continue

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

    st.session_state.data_foco_calendario = dt_inicio.strftime("%Y-%m-%d")

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

    ult = st.session_state.get("ultimo_evento_criado")
    if ult and ult.get("event_id") == event_id:
        st.session_state.ultimo_evento_criado = None


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


# --- JANELA MODAL DE PERFIL (NOME E FOTO) ---
@st.dialog("Meu Perfil")
def abrir_modal_perfil():
    perfil_atual = carregar_perfil_usuario()
    st.caption("Personalize o nome e a foto exibidos no topo do painel.")

    novo_nome = st.text_input(
        "Seu nome de exibição",
        value=perfil_atual["nome"],
        placeholder="Ex.: José Mário Júnior",
    )

    nova_foto = st.file_uploader(
        "Escolher foto do computador ou celular",
        accept_multiple_files=False,
        help="Aceita JPG, JPEG, PNG, WEBP ou JFIF.",
    )

    preview_b64 = ""
    erro_imagem = None
    if nova_foto is not None:
        try:
            preview_b64 = processar_imagem_avatar(nova_foto)
        except Exception as e:
            erro_imagem = f"Arquivo inválido. Selecione uma imagem (JPG, PNG ou WEBP). Detalhe: {e}"
    elif perfil_atual["avatar_b64"]:
        preview_b64 = perfil_atual["avatar_b64"]

    if erro_imagem:
        st.error(erro_imagem)
    elif preview_b64:
        st.markdown(
            f'<div style="display:flex; align-items:center; gap:0.85rem; margin: 0.5rem 0;">'
            f'<img src="data:image/jpeg;base64,{preview_b64}" class="perfil-avatar" style="width:84px; height:84px; min-width:84px;">'
            f'<span style="font-size:0.9rem; opacity:0.85;">Pré-visualização do avatar</span>'
            f"</div>",
            unsafe_allow_html=True,
        )

    remover_foto = st.checkbox("Remover foto atual", value=False)

    st.write("")
    if st.button("Salvar Perfil", type="primary", use_container_width=True):
        if erro_imagem:
            st.warning("Corrija o arquivo de imagem antes de salvar.")
        else:
            try:
                with st.spinner("Salvando perfil..."):
                    avatar_final = "" if remover_foto else preview_b64
                    _gravar_evento_cofre(
                        nome_exibicao=novo_nome.strip(),
                        avatar_b64=avatar_final,
                    )
                st.success("Perfil atualizado!")
                time_module.sleep(0.7)
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao salvar perfil: {e}")


# --- JANELA MODAL DE ALTERAÇÃO DE SENHA ---
@st.dialog("Alterar Senha de Acesso")
def abrir_modal_alterar_senha():
    st.caption("A nova senha ficará salva permanentemente na sua conta.")
    with st.form("form_alterar_senha_interna", clear_on_submit=True):
        senha_atual = st.text_input("Senha atual", type="password")
        nova_senha = st.text_input("Nova senha (mín. 4 caracteres)", type="password")
        confirmar_nova = st.text_input("Confirmar nova senha", type="password")
        confirmar_btn = st.form_submit_button("Atualizar Senha", type="primary", use_container_width=True)

        if confirmar_btn:
            if not validar_senha_usuario(senha_atual):
                st.error("A senha atual está incorreta.")
            elif len(nova_senha) < 4:
                st.warning("A nova senha deve ter pelo menos 4 caracteres.")
            elif nova_senha != confirmar_nova:
                st.warning("A confirmação não coincide com a nova senha.")
            else:
                try:
                    with st.spinner("Salvando nova senha..."):
                        salvar_nova_senha_cofre(nova_senha)
                    st.success("Senha atualizada com sucesso!")
                    time_module.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro ao salvar senha: {e}")


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

    destino_maps = local_txt if local_txt else f"{titulo_ev}, Feira de Santana - BA"

    st.subheader(titulo_ev)
    st.caption(f"Agenda: **{categoria_ev}**")

    st.markdown("---")
    c_ini, c_fim = st.columns(2)
    with c_ini:
        with st.container(border=True):
            st.markdown(f"**Início**\n\n{inicio_fmt}")
    with c_fim:
        with st.container(border=True):
            st.markdown(f"**Término**\n\n{fim_fmt}")

    if local_txt:
        with st.container(border=True):
            st.markdown(f"**Endereço / Localização**\n\n{local_txt}")

    with st.container(border=True):
        st.markdown("**Observações / Detalhes**")
        if descricao_txt:
            st.write(descricao_txt)
        else:
            st.caption("Nenhuma observação adicional registrada neste evento.")

    st.write("")
    col_maps, col_del = st.columns(2)
    with col_maps:
        st.link_button(
            "Abrir Rota no Google Maps",
            url=gerar_link_rota_maps(destino_maps),
            type="primary",
            use_container_width=True,
        )
    with col_del:
        if st.button("Excluir Evento", type="secondary", use_container_width=True):
            try:
                excluir_evento(props["calendarId"], ev_clicado["id"])
                st.toast("Evento removido do Google Agenda!")
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao excluir: {e}")


# --- CARREGA EVENTOS E PERFIL ---
eventos = carregar_eventos()
perfil = carregar_perfil_usuario()
ids_eventos_ativos = {e["id"] for e in eventos}

if (
    st.session_state.ultimo_evento_criado
    and st.session_state.ultimo_evento_criado.get("event_id") not in ids_eventos_ativos
):
    st.session_state.ultimo_evento_criado = None


# --- BARRA LATERAL ---
with st.sidebar:
    # Ações de Conta no topo da barra lateral (apenas texto legível)
    if st.button("Perfil", use_container_width=True):
        abrir_modal_perfil()
    if st.button("Alterar Senha", use_container_width=True):
        abrir_modal_alterar_senha()
    if st.button("Sair", use_container_width=True):
        st.session_state["autenticado"] = False
        st.rerun()

    st.markdown("---")
    st.header("Criação Rápida com IA")
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
        if st.form_submit_button("Agendar Texto", use_container_width=True):
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
        msg_sucesso = f"**{ult['titulo']}** ({ult['categoria']}) — {ult['inicio_fmt']}"
        if ult.get("localizacao"):
            msg_sucesso += f"\n\nLocal: {ult['localizacao']}"
        st.success(msg_sucesso)

        if st.button("Desfazer Último Evento", type="secondary", use_container_width=True):
            try:
                excluir_evento(ult["calendar_id"], ult["event_id"])
                st.session_state.ultimo_evento_criado = None
                st.toast("Último evento removido!")
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao desfazer: {e}")

    st.markdown("---")
    st.subheader("Filtros de Visualização")
    filtro_cat = st.radio(
        "Exibir:",
        ["Todas", "Profissional", "Pessoal"],
        horizontal=True,
    )

    if st.button("Sincronizar Google Agenda", use_container_width=True):
        carregar_eventos.clear()
        carregar_perfil_usuario.clear()
        st.session_state.data_foco_calendario = datetime.now(TZ).strftime("%Y-%m-%d")
        st.session_state.cal_reset_counter += 1
        st.rerun()

    # 3. Formulário Manual Completo
    with st.expander("Cadastro Manual Detalhado"):
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


# --- ÁREA PRINCIPAL: CABEÇALHO FIXO (STICKY) ---
hoje_str = datetime.now(TZ).strftime("%Y-%m-%d")
eventos_hoje = [e for e in eventos if e["start"].startswith(hoje_str)]
prof_count = sum(1 for e in eventos if e["extendedProps"]["categoria"] == "Profissional")
pess_count = sum(1 for e in eventos if e["extendedProps"]["categoria"] == "Pessoal")

nome_usuario = perfil["nome"].strip()
avatar_b64 = perfil["avatar_b64"].strip()

with st.container():
    st.markdown('<div id="sticky-header-anchor"></div>', unsafe_allow_html=True)

    if nome_usuario or avatar_b64:
        if avatar_b64:
            avatar_html = f'<img src="data:image/jpeg;base64,{avatar_b64}" class="perfil-avatar" alt="Foto do usuário">'
        else:
            inicial = nome_usuario[0].upper() if nome_usuario else "U"
            avatar_html = f'<div class="perfil-avatar-placeholder">{inicial}</div>'

        texto_nome = nome_usuario if nome_usuario else "Bem-vindo"
        st.markdown(
            f"""
            <div class="perfil-header">
                {avatar_html}
                <div class="perfil-texto">
                    <p class="perfil-saudacao">Olá,</p>
                    <p class="perfil-nome">{texto_nome}</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.title("My Diaries")

    k1, k2, k3 = st.columns(3)
    k1.metric("Compromissos Hoje", len(eventos_hoje))
    k2.metric("Profissional (Período)", prof_count)
    k3.metric("Pessoal (Período)", pess_count)


# --- CONTEÚDO ROLÁVEL: BARRA RÁPIDA E CALENDÁRIO ---
with st.expander("Novo agendamento rápido (Voz ou Texto)", expanded=False):
    col_mob_voz, col_mob_txt = st.columns([1, 1.3])
    with col_mob_voz:
        audio_mob = st.audio_input("Gravar áudio", key="mic_input_mobile")
        if audio_mob is not None:
            raw_bytes_mob = audio_mob.getvalue()
            audio_hash_mob = hashlib.md5(raw_bytes_mob).hexdigest()
            if audio_hash_mob != st.session_state.ultimo_audio_hash:
                st.session_state.ultimo_audio_hash = audio_hash_mob
                with st.spinner("Ouvindo e agendando..."):
                    try:
                        ev_extraido = processar_entrada_ia(audio_bytes=raw_bytes_mob)
                        salvar_evento_extraido(ev_extraido)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erro ao processar áudio: {e}")

    with col_mob_txt:
        with st.form("form_texto_rapido_mobile", clear_on_submit=True):
            texto_mob = st.text_input(
                "Ou escreva o compromisso:",
                placeholder="Ex.: Dentista sexta às 15h",
            )
            if st.form_submit_button("Agendar Agora", type="primary", use_container_width=True):
                if texto_mob.strip():
                    with st.spinner("Agendando..."):
                        try:
                            ev_extraido = processar_entrada_ia(texto=texto_mob.strip())
                            salvar_evento_extraido(ev_extraido)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro ao interpretar: {e}")

if filtro_cat != "Todas":
    eventos_filtrados = [e for e in eventos if e["extendedProps"]["categoria"] == filtro_cat]
else:
    eventos_filtrados = eventos

# Configuração responsiva do FullCalendar para caber perfeitamente no celular e no PC
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
    "height": "auto",
    "contentHeight": "auto",
    "handleWindowResize": True,
    "dayMaxEvents": 2,
}

custom_css_calendario = """
    .fc .fc-toolbar {
        flex-wrap: wrap;
        gap: 0.4rem;
    }
    .fc .fc-toolbar-title {
        font-size: 1.15rem !important;
    }
    .fc .fc-button {
        padding: 0.25rem 0.55rem !important;
        font-size: 0.85rem !important;
    }
    .fc .fc-daygrid-day-frame {
        min-height: 58px !important;
    }
    @media (max-width: 600px) {
        .fc .fc-toolbar-title {
            font-size: 1rem !important;
        }
        .fc .fc-col-header-cell-cushion {
            font-size: 0.75rem !important;
            padding: 2px !important;
        }
        .fc .fc-daygrid-day-number {
            font-size: 0.75rem !important;
            padding: 2px 4px !important;
        }
        .fc-event-title, .fc-event-time {
            font-size: 0.7rem !important;
        }
    }
"""

estado_calendario = calendar(
    events=eventos_filtrados,
    options=calendar_options,
    custom_css=custom_css_calendario,
    callbacks=["eventClick"],
    key=f"cal_{filtro_cat}_{st.session_state.data_foco_calendario}_{st.session_state.cal_reset_counter}",
)

# --- ABERTURA DO POP-UP AO CLICAR NO EVENTO (MÊS OU LISTA) ---
if estado_calendario and estado_calendario.get("eventClick"):
    ev_clicado = estado_calendario["eventClick"]["event"]
    st.session_state.cal_reset_counter += 1
    abrir_modal_evento(ev_clicado)