import sys
import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import streamlit as st
import requests
import pandas as pd

API_URL = "http://127.0.0.1:8000"
TIMEOUT = 5
CHAVE_TABELA = "tabela_livros_principal"
CENTAVOS = Decimal("0.01")
ANO_MINIMO_PUBLICACAO = 1450
ANO_ATUAL = datetime.now().year
# O valor-padrão inicializa o estado local; o prazo efetivo vem do backend.
SESSION_IDLE_SECONDS = 30 * 60

CAMPOS_LIVRO = {"id", "titulo", "autor", "ano_publicacao", "preco", "estoque"}

st.set_page_config(page_title="Gerenciador da Biblioteca", page_icon="📚", layout="wide")

st.markdown("""
<style>
.block-container {
    padding-top: 2.2rem;
    padding-bottom: 0.8rem;
}

h1 {
    font-size: 1.75rem !important;
    margin-top: 0 !important;
    margin-bottom: 0.4rem !important;
}

h2, h3 {
    font-size: 1.25rem !important;
    margin-top: 0.5rem !important;
    margin-bottom: 0.35rem !important;
}

div[data-testid="stVerticalBlock"] {
    gap: 0.45rem;
}
</style>
""", unsafe_allow_html=True)


def headers_jwt():
    token = st.session_state.get("token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def api_online():
    """Faz um health check rápido, sem depender de uma chamada protegida."""
    try:
        response = requests.get(API_URL, timeout=2)

        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def possui_scope(scope):
    usuario = st.session_state.get("usuario") or {}

    return scope in usuario.get("scopes", [])


def limpar_edicao():
    st.session_state.id_carregado_formulario = None

    for chave in ("edit_titulo", "edit_autor", "edit_ano", "edit_preco", "edit_estoque"):
        st.session_state.pop(chave, None)


def limpar_selecao():
    st.session_state.id_selecionado = None
    limpar_edicao()


def agendar_limpeza_edicao():
    """Limpa widgets no início do próximo rerun, antes de recriá-los."""
    st.session_state.id_selecionado = None
    st.session_state.limpar_edicao_pendente = True


def limpar_sessao():
    st.session_state.token = None
    st.session_state.refresh_token = None
    st.session_state.usuario = None
    st.session_state.sessao_id = None
    st.session_state.id_selecionado = None
    st.session_state.access_expira_em = None
    st.session_state.refresh_expira_em = None
    st.session_state.horario_expiracao = None
    st.session_state.session_idle_seconds = SESSION_IDLE_SECONDS
    st.session_state.id_carregado_formulario = None
    st.session_state.limpar_edicao_pendente = True


def registrar_atividade():
    if not st.session_state.get("token"):
        return

    st.session_state.horario_expiracao = (
        datetime.now(timezone.utc) + timedelta(seconds=st.session_state.session_idle_seconds)
    )


def salvar_tokens(dados):
    agora = datetime.now(timezone.utc)
    access_seconds = int(dados["expires_in"])
    refresh_seconds = int(dados["refresh_expires_in"])
    idle_seconds = int(dados["session_idle_seconds"])

    st.session_state.token = dados["access_token"]
    st.session_state.refresh_token = dados["refresh_token"]
    st.session_state.usuario = dados["usuario"]
    st.session_state.sessao_id = dados["usuario"]["sessao_id"]
    st.session_state.access_expira_em = agora + timedelta(seconds=access_seconds)
    st.session_state.refresh_expira_em = agora + timedelta(seconds=refresh_seconds)
    st.session_state.session_idle_seconds = idle_seconds
    registrar_atividade()


def resposta_token_valida(dados):
    if not isinstance(dados, dict):
        return False

    usuario = dados.get("usuario")

    return (
        isinstance(dados.get("access_token"), str)
        and bool(dados["access_token"])
        and isinstance(dados.get("refresh_token"), str)
        and bool(dados["refresh_token"])
        and str(dados.get("token_type", "")).lower() == "bearer"
        and type(dados.get("expires_in")) is int
        and dados["expires_in"] > 0
        and type(dados.get("refresh_expires_in")) is int
        and dados["refresh_expires_in"] > 0
        and type(dados.get("session_idle_seconds")) is int
        and dados["session_idle_seconds"] > 0
        and isinstance(usuario, dict)
        and {"id", "username", "role", "scopes", "sessao_id"}.issubset(usuario)
        and type(usuario.get("id")) is int
        and isinstance(usuario.get("username"), str)
        and isinstance(usuario.get("role"), str)
        and isinstance(usuario.get("scopes"), list)
        and all(isinstance(scope, str) for scope in usuario["scopes"])
        and isinstance(usuario.get("sessao_id"), str)
        and bool(usuario["sessao_id"])
    )


def logout_backend():
    refresh_token = st.session_state.get("refresh_token")
    if not refresh_token:
        return

    try:
        requests.post(f"{API_URL}/logout", json={"refresh_token": refresh_token}, timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        pass


def logout():
    try:
        logout_backend()
    finally:
        limpar_sessao()


def renovar_token():
    refresh_token = st.session_state.get("refresh_token")
    if not refresh_token:
        return None

    try:
        response = requests.post(f"{API_URL}/refresh", json={"refresh_token": refresh_token}, timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        return None

    if response.status_code == 200:
        try:
            dados = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            return response

        if resposta_token_valida(dados):
            salvar_tokens(dados)

    # Devolve o objeto HTTP para preservar o status e o detalhe da falha.
    return response


def detalhe_resposta(response):
    try:
        corpo = response.json()
        if isinstance(corpo, dict):
            return corpo.get("detail", corpo)

        return corpo
    except (ValueError, requests.exceptions.JSONDecodeError):
        return response.text or "A API não retornou detalhes do erro."


def mostrar_erro_api(response):
    detalhe = detalhe_resposta(response)

    if response.status_code == 401:
        st.error(f"🔒 {detalhe}")
    elif response.status_code == 403:
        st.error(f"⛔ {detalhe}")
    else:
        st.error(f"Erro {response.status_code}: {detalhe}")


def requisitar_api(metodo, caminho, *, autenticada=False, verificar_status=True, **kwargs):
    cabecalhos_base = dict(kwargs.pop("headers", {}) or {})

    def enviar():
        cabecalhos = dict(cabecalhos_base)
        if autenticada:
            cabecalhos.update(headers_jwt())

        return requests.request(
            metodo,
            f"{API_URL}{caminho}",
            headers=cabecalhos,
            timeout=TIMEOUT,
            **kwargs,
        )

    try:
        response = enviar()

        if autenticada and response.status_code == 401:
            token_anterior = st.session_state.get("token")
            resposta_refresh = renovar_token()

            renovou = (
                resposta_refresh is not None
                and resposta_refresh.status_code == 200
                and st.session_state.get("token") != token_anterior
            )

            if renovou:
                response = enviar()

            if not renovou or response.status_code == 401:
                if renovou and response.status_code == 401:
                    detalhe = detalhe_resposta(response)
                elif resposta_refresh is None:
                    detalhe = "Não foi possível obter resposta do endpoint /refresh."
                elif not renovou and resposta_refresh.status_code == 200:
                    detalhe = (
                        "O endpoint /refresh respondeu 200, mas o payload de tokens "
                        "estava inválido ou incompleto."
                    )
                else:
                    detalhe = detalhe_resposta(resposta_refresh)

                logout_backend()
                limpar_sessao()
                st.error(
                    "🔒 Sua sessão expirou, o refresh token foi invalidado ou "
                    "a conta não está mais disponível. "
                    f"Detalhe da API: {detalhe}. Você será desconectado."
                )
                time.sleep(2)
                st.rerun()
    except requests.exceptions.ConnectionError:
        st.error("Não foi possível conectar à API.")
        return None
    except requests.exceptions.Timeout:
        st.error("A API demorou muito para responder.")
        return None
    except requests.exceptions.RequestException as erro:
        st.error(f"Erro de comunicação com a API: {erro}")
        return None

    if verificar_status and not response.ok:
        mostrar_erro_api(response)

    return response


def validar_resposta_json(
    response,
    status_esperado=200,
    *,
    contexto="a resposta da API",
    campos_obrigatorios=(),
    tipo_esperado=dict,
):
    """Valida status, JSON e estrutura mínima esperada pela chamada."""
    if response is None:
        return None

    if response.status_code != status_esperado:
        mostrar_erro_api(response)

        return None

    try:
        dados = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        st.error(f"A API retornou JSON inválido para {contexto}.")
        return None
    if tipo_esperado is not None and not isinstance(dados, tipo_esperado):
        st.error(f"A API retornou um formato inesperado para {contexto}.")
        return None
    if isinstance(dados, dict):
        ausentes = [campo for campo in campos_obrigatorios if campo not in dados]
        if ausentes:
            st.error(f"A resposta de {contexto} está incompleta; faltam: {', '.join(ausentes)}.")
            return None

    return dados


def valor_decimal(valor):
    try:
        resultado = Decimal(str(valor)).quantize(
            CENTAVOS,
            rounding=ROUND_HALF_UP,
        )

        return resultado if resultado.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def formatar_moeda(moeda, valor):
    return f"{moeda} {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_tempo(segundos):
    segundos = max(0, int(segundos))
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    return f"{horas:02d}:{minutos:02d}:{segundos:02d}" if horas else f"{minutos:02d}:{segundos:02d}"


def validar_livro_formulario(titulo, autor, ano, preco):
    """Validação local; a API repete as regras e é a fonte de verdade."""
    if not titulo.strip():
        return "O título não pode ficar vazio ou conter apenas espaços."
    if not autor.strip():
        return "O autor não pode ficar vazio ou conter apenas espaços."
    if not ANO_MINIMO_PUBLICACAO <= int(ano) <= ANO_ATUAL:
        return f"Informe um ano de publicação entre {ANO_MINIMO_PUBLICACAO} e {ANO_ATUAL}."
    if preco is None or preco <= 0:
        return "Informe um preço válido maior que zero."
    return None


@st.fragment(run_every="10s")
def painel_expiracao():
    if not st.session_state.get("token"):
        return

    agora = datetime.now(timezone.utc)
    horario_expiracao = st.session_state.get("horario_expiracao")
    if horario_expiracao is None:
        return

    restante_inatividade = (horario_expiracao - agora).total_seconds()
    access_expira_em = st.session_state.get("access_expira_em")
    refresh_expira_em = st.session_state.get("refresh_expira_em")
    restante_access = (access_expira_em - agora).total_seconds() if access_expira_em else 0
    restante_refresh = (refresh_expira_em - agora).total_seconds() if refresh_expira_em else 0

    st.markdown("### ⏳ Sessão")
    st.metric("Expira por inatividade em", formatar_tempo(restante_inatividade))

    if restante_access > 0:
        st.caption(
            f"🔑 Access token expira em {formatar_tempo(restante_access)} — "
            "renovação automática na próxima chamada protegida."
        )
    else:
        st.caption("⌛ Access token expirado. Será renovado na próxima chamada protegida.")

    st.caption(f"🔄 Refresh token: {max(0, restante_refresh / 86400):.1f} dias restantes.")

    if 0 < restante_inatividade <= 300:
        st.warning("A sessão será encerrada em menos de 5 minutos.")

    if restante_inatividade <= 0:
        st.error("⛔ Sessão encerrada por inatividade. Redirecionando...")
        time.sleep(2)
        logout()
        st.rerun()


# Inicialização do estado e limpeza segura de widgets antes de renderizá-los.
for chave, valor_inicial in (
    ("token", None),
    ("refresh_token", None),
    ("usuario", None),
    ("sessao_id", None),
    ("id_selecionado", None),
    ("id_carregado_formulario", None),
    ("limpar_edicao_pendente", False),
    ("geracao_tabela", 0),
    ("chave_tabela_anterior", None),
    ("access_expira_em", None),
    ("refresh_expira_em", None),
    ("horario_expiracao", None),
    ("session_idle_seconds", SESSION_IDLE_SECONDS),
):
    if chave not in st.session_state:
        st.session_state[chave] = valor_inicial

if st.session_state.limpar_edicao_pendente:
    limpar_edicao()
    st.session_state.limpar_edicao_pendente = False

# Health check curto para dar ao operador um sinal visível sobre a API.
if api_online():
    st.sidebar.success("🟢 API online")
else:
    st.sidebar.error("🔴 API indisponível")

# LOGIN / CADASTRO
if not st.session_state.token:
    st.title("🔐 Sistema da Biblioteca")
    col_login, col_cadastro = st.columns(2)

    with col_login:
        st.subheader("Entrar")

        with st.form("form_login"):
            username = st.text_input("Usuário")
            senha = st.text_input("Senha", type="password")
            entrar = st.form_submit_button("🔓 Entrar", type="primary", width="stretch")

        if entrar:
            if not username.strip() or not senha:
                st.error("Informe usuário e senha.")
            else:
                response = requisitar_api(
                    "POST",
                    "/token",
                    verificar_status=False,
                    data={"username": username.strip(), "password": senha},
                )

                dados = validar_resposta_json(
                    response,
                    contexto="o login",
                    campos_obrigatorios=(
                        "access_token", "refresh_token", "token_type",
                        "expires_in", "refresh_expires_in",
                        "session_idle_seconds", "usuario",
                    ),
                )

                if dados is not None:
                    if resposta_token_valida(dados):
                        salvar_tokens(dados)
                        st.rerun()
                    else:
                        st.error("A API retornou dados de login incompletos ou inválidos.")

    with col_cadastro:
        st.subheader("Criar usuário")

        with st.form("form_usuario"):
            novo_usuario = st.text_input("Novo usuário")
            nova_senha = st.text_input("Nova senha", type="password")
            confirmar = st.text_input("Confirmar senha", type="password")
            cadastrar_usuario = st.form_submit_button("➕ Criar usuário", width="stretch")

        if cadastrar_usuario:
            if not novo_usuario.strip():
                st.error("Informe o nome do usuário.")
            elif nova_senha != confirmar:
                st.error("As senhas não coincidem.")
            elif len(nova_senha) < 6:
                st.error("A senha deve possuir pelo menos 6 caracteres.")
            else:
                response = requisitar_api(
                    "POST",
                    "/usuarios",
                    verificar_status=False,
                    json={"username": novo_usuario.strip(), "senha": nova_senha},
                )

                dados_cadastro = validar_resposta_json(
                    response,
                    status_esperado=201,
                    contexto="o cadastro do usuário",
                    campos_obrigatorios=("mensagem", "id", "username", "role"),
                )

                if dados_cadastro is not None:
                    st.success(dados_cadastro["mensagem"])
    st.stop()

# Perfil e permissões são renovados/validados pela API em cada execução do painel.
response = requisitar_api("GET", "/me", autenticada=True, verificar_status=False)
if response is None:
    st.stop()

usuario_atualizado = validar_resposta_json(
    response,
    contexto="o perfil do usuário",
    campos_obrigatorios=("id", "username", "role", "scopes", "sessao_id"),
)

if (
    not isinstance(usuario_atualizado, dict)
    or type(usuario_atualizado.get("id")) is not int
    or not isinstance(usuario_atualizado.get("username"), str)
    or not isinstance(usuario_atualizado.get("role"), str)
    or not isinstance(usuario_atualizado.get("scopes"), list)
    or not all(isinstance(scope, str) for scope in usuario_atualizado.get("scopes", []))
    or not isinstance(usuario_atualizado.get("sessao_id"), str)
    or not usuario_atualizado.get("sessao_id")
):
    st.error("A API retornou um perfil ou uma lista de permissões inválidos.")
    st.stop()

if st.session_state.get("sessao_id") != usuario_atualizado["sessao_id"]:
    st.error("A sessão retornada pela API não corresponde à sessão local. Entre novamente.")
    logout()
    st.rerun()

st.session_state.usuario = usuario_atualizado
st.session_state.sessao_id = usuario_atualizado["sessao_id"]

# A API confirmou a sessão ativa neste rerun, então o contador local acompanha
# a atividade que acabou de ser registrada no backend.
registrar_atividade()
usuario = usuario_atualizado

st.title("📚 Painel Gerenciador da Biblioteca")
st.sidebar.header("👤 Usuário")
st.sidebar.write(f"**Login:** {usuario.get('username', '—')}")
st.sidebar.write(f"**Perfil:** {usuario.get('role', '—')}")

if usuario.get("role") == "admin":
    st.sidebar.success("Administrador")
else:
    st.sidebar.info("Usuário comum")

st.sidebar.markdown("**Permissões:**")

for scope in usuario.get("scopes", []):
    st.sidebar.write(f"✓ {scope}")

with st.sidebar:
    painel_expiracao()

if st.sidebar.button("🚪 Sair", width="stretch"):
    logout()
    st.rerun()

# INDICADORES
response_valor = requisitar_api(
    "GET", "/livros/valor-acervo", autenticada=True, verificar_status=False
)

if response_valor is None:
    st.stop()

valor_acervo = validar_resposta_json(
    response_valor,
    contexto="o valor do acervo",
    campos_obrigatorios=("valor_total", "moeda"),
)

if valor_acervo is None:
    st.stop()

total_acervo = valor_decimal(valor_acervo["valor_total"])
if total_acervo is None:
    st.error("A API retornou o valor do acervo em formato inválido.")
    st.stop()

response_quantidade = requisitar_api(
    "GET", "/livros/quantidade", autenticada=True, verificar_status=False
)

if response_quantidade is None:
    st.stop()

quantidade_json = validar_resposta_json(
    response_quantidade,
    contexto="a quantidade de livros",
    campos_obrigatorios=("quantidade_titulos", "quantidade_exemplares"),
)

if quantidade_json is None:
    st.stop()

try:
    total_titulos = int(quantidade_json["quantidade_titulos"])
    total_exemplares = int(quantidade_json["quantidade_exemplares"])
except (TypeError, ValueError):
    st.error("A API retornou a quantidade de livros em formato inválido.")
    st.stop()

col_m1, col_m2, col_m3 = st.columns(3)
col_m1.metric("Títulos cadastrados", total_titulos)
col_m2.metric("Exemplares em estoque", total_exemplares)
col_m3.metric("Valor do acervo", formatar_moeda(valor_acervo["moeda"], total_acervo))

# BUSCA
st.markdown("### 🔍 Livros Cadastrados")
col_texto, col_estoque = st.columns([3, 1])
with col_texto:
    busca_texto = st.text_input(
        "Filtrar por título ou autor",
        label_visibility="collapsed",
        placeholder="🔎 Filtrar por título ou autor",
        on_change=registrar_atividade,
    )
with col_estoque:
    busca_estoque = st.number_input(
        "Estoque mínimo",
        min_value=0,
        value=0,
        step=1,
        label_visibility="collapsed",
        help="Estoque mínimo",
        on_change=registrar_atividade,
    )

params = {}
if busca_texto.strip():
    params["texto"] = busca_texto.strip()
if busca_estoque > 0:
    params["min_estoque"] = int(busca_estoque)

response_busca = requisitar_api(
    "GET", "/livros/busca", autenticada=True,
    verificar_status=False, params=params,
)

if response_busca is None:
    st.stop()

dados_livros = validar_resposta_json(response_busca, contexto="a lista de livros")

if not isinstance(dados_livros, dict):
    st.error("A API retornou uma lista de livros em formato inválido.")
    st.stop()

total_encontrado = len(dados_livros)
st.caption(
    f"{total_encontrado} "
    f"{'registro encontrado' if total_encontrado == 1 else 'registros encontrados'}"
)

if dados_livros:
    dados_lista = []

    try:
        for id_livro, dados in dados_livros.items():
            if not isinstance(dados, dict):
                raise ValueError("estrutura inválida")

            preco = valor_decimal(dados["preco"])
            estoque = int(dados["estoque"])

            if preco is None:
                raise ValueError("preço inválido")

            dados_lista.append({
                "ID": int(id_livro),
                "Título": str(dados["titulo"]),
                "Autor": str(dados["autor"]),
                "Ano": int(dados["ano_publicacao"]),
                "Preço (R$)": float(preco),
                "Estoque": estoque,
                "Valor em Estoque (R$)": float(preco * estoque),
            })
    except (KeyError, TypeError, ValueError, InvalidOperation):
        st.error("A API retornou dados inválidos para um dos livros.")
        st.stop()

    df = pd.DataFrame(dados_lista)
    ids_visiveis = [int(valor) for valor in df["ID"].tolist()]
    chave_ids = "_".join(map(str, ids_visiveis))
    chave_tabela_atual = f"{CHAVE_TABELA}_{chave_ids}_{st.session_state.geracao_tabela}"

    if st.session_state.chave_tabela_anterior != chave_tabela_atual:
        if st.session_state.id_selecionado not in ids_visiveis:
            limpar_selecao()
        st.session_state.chave_tabela_anterior = chave_tabela_atual

    evento = st.dataframe(
        df,
        key=chave_tabela_atual,
        width="stretch",
        height=210,
        row_height=32,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-cell",
        column_config={
            "ID": st.column_config.NumberColumn("ID", width="small"),
            "Título": st.column_config.TextColumn("Título", width="large"),
            "Autor": st.column_config.TextColumn("Autor", width="medium"),
            "Ano": st.column_config.NumberColumn("Ano", width="small", format="%d"),
            "Preço (R$)": st.column_config.NumberColumn("Preço (R$)", format="R$ %.2f"),
            "Estoque": st.column_config.NumberColumn("Estoque", width="small"),
            "Valor em Estoque (R$)": st.column_config.NumberColumn(
                "Valor em Estoque (R$)", format="R$ %.2f"
            ),
        },
    )

    celulas = evento.selection.cells
    if celulas:
        indice = celulas[0][0]

        if isinstance(indice, int) and 0 <= indice < len(df):
            id_clicado = int(df.iloc[indice]["ID"])

            if st.session_state.id_selecionado != id_clicado:
                st.session_state.id_selecionado = id_clicado
                limpar_edicao()
                registrar_atividade()
else:
    st.warning("Nenhum livro encontrado.")
    limpar_selecao()
    st.session_state.chave_tabela_anterior = None

# ALTERAÇÃO / EXCLUSÃO
id_selecionado = st.session_state.get("id_selecionado")
if id_selecionado is not None:
    response_livro = requisitar_api(
        "GET", f"/livros/{id_selecionado}",
        autenticada=True, verificar_status=False,
    )

    if response_livro is None:
        st.stop()

    if response_livro.status_code == 404:
        agendar_limpeza_edicao()
        st.session_state.aviso_livro_removido = True
        st.rerun()

    dados_atuais = validar_resposta_json(
        response_livro,
        contexto="o livro selecionado",
        campos_obrigatorios=tuple(sorted(CAMPOS_LIVRO)),
    )

    if dados_atuais is None:
        st.stop()

    if st.session_state.get("id_carregado_formulario") != id_selecionado:
        preco_atual = valor_decimal(dados_atuais.get("preco"))

        try:
            estoque_atual = int(dados_atuais["estoque"])
            ano_atual_livro = int(dados_atuais["ano_publicacao"])
        except (TypeError, ValueError):
            estoque_atual = ano_atual_livro = None

        if preco_atual is None or estoque_atual is None or ano_atual_livro is None:
            st.error("A API retornou valores inválidos para o livro selecionado.")
            st.stop()

        st.session_state.id_carregado_formulario = id_selecionado
        st.session_state.edit_titulo = dados_atuais.get("titulo", "")
        st.session_state.edit_autor = dados_atuais.get("autor", "")
        st.session_state.edit_ano = ano_atual_livro
        st.session_state.edit_preco = float(preco_atual)
        st.session_state.edit_estoque = estoque_atual

    st.markdown(f"### ✏️ Alterar Livro — ID {id_selecionado}")
    pode_excluir = possui_scope("livros:delete")

    with st.form("form_edicao"):
        c1, c2 = st.columns([3, 2])
        with c1:
            novo_titulo = st.text_input("Título", key="edit_titulo")
        with c2:
            novo_autor = st.text_input("Autor", key="edit_autor")

        colunas = [1, 1.2, 1, 1.15, 1.15] if pode_excluir else [1, 1.2, 1, 1.3]
        if pode_excluir:
            c3, c4, c5, c6, c7 = st.columns(colunas)
        else:
            c3, c4, c5, c6 = st.columns(colunas)
        with c3:
            novo_ano = st.number_input(
                "Ano", min_value=ANO_MINIMO_PUBLICACAO, max_value=ANO_ATUAL,
                step=1, key="edit_ano",
            )
        with c4:
            novo_preco = st.number_input(
                "Preço (R$)", min_value=0.01, step=1.0,
                format="%.2f", key="edit_preco",
            )
        with c5:
            novo_estoque = st.number_input("Estoque", min_value=0, step=1, key="edit_estoque")
        with c6:
            st.write("")
            st.write("")
            salvar = st.form_submit_button(
                "💾 Salvar", type="primary", width="stretch",
                disabled=not possui_scope("livros:update"),
            )

        excluir = False

        if pode_excluir:
            with c7:
                st.write("")
                st.write("")
                excluir = st.form_submit_button("🗑️ Excluir", width="stretch")

    if salvar:
        registrar_atividade()
        preco_decimal = valor_decimal(novo_preco)
        erro = validar_livro_formulario(novo_titulo, novo_autor, novo_ano, preco_decimal)

        if erro:
            st.error(erro)
        else:
            payload = {
                "titulo": novo_titulo.strip(),
                "autor": novo_autor.strip(),
                "ano_publicacao": int(novo_ano),
                "preco": format(preco_decimal, ".2f"),
                "estoque": int(novo_estoque),
            }

            response = requisitar_api(
                "PUT", f"/livros/{id_selecionado}",
                autenticada=True, verificar_status=False, json=payload,
            )

            resultado = validar_resposta_json(
                response,
                contexto="a confirmação da edição",
                campos_obrigatorios=("mensagem", "livro"),
            )

            if resultado is not None:
                livro_resultado = resultado["livro"]
                if not isinstance(livro_resultado, dict) or not CAMPOS_LIVRO.issubset(livro_resultado):
                    st.error("A API confirmou a edição, mas retornou o livro incompleto.")
                else:
                    st.success(resultado["mensagem"])
                    st.session_state.geracao_tabela += 1
                    agendar_limpeza_edicao()
                    time.sleep(1)
                    st.rerun()

    if excluir:
        registrar_atividade()
        response = requisitar_api(
            "DELETE", f"/livros/{id_selecionado}",
            autenticada=True, verificar_status=False,
        )

        resultado = validar_resposta_json(
            response,
            contexto="a confirmação da exclusão",
            campos_obrigatorios=("mensagem",),
        )

        if resultado is not None:
            st.success(resultado["mensagem"])
            st.session_state.geracao_tabela += 1
            agendar_limpeza_edicao()
            time.sleep(3)
            st.rerun()
else:
    st.caption("Selecione um livro na tabela para editar ou excluir.")

# Avisos de estado são exibidos após o rerun que limpou a seleção.
if st.session_state.get("aviso_livro_removido"):
    st.warning("O livro selecionado foi removido. Selecione outro livro.")
    st.session_state.aviso_livro_removido = False

# NOVO LIVRO
if possui_scope("livros:create"):
    st.markdown("### ➕ Novo Livro")
    with st.form("form_cadastro", clear_on_submit=True):
        c1, c2 = st.columns([3, 2])
        with c1:
            titulo = st.text_input("Título")
        with c2:
            autor = st.text_input("Autor")

        c3, c4, c5, c6 = st.columns([1, 1.2, 1, 1.5])
        with c3:
            ano = st.number_input(
                "Ano de Publicação", min_value=ANO_MINIMO_PUBLICACAO,
                max_value=ANO_ATUAL, value=ANO_ATUAL, step=1,
            )
        with c4:
            preco = st.number_input(
                "Preço (R$)", min_value=0.01, value=1.00,
                step=1.0, format="%.2f",
            )
        with c5:
            estoque = st.number_input("Estoque", min_value=0, value=1, step=1)
        with c6:
            st.write("")
            st.write("")
            cadastrar = st.form_submit_button("➕ Cadastrar", type="primary", width="stretch")

    if cadastrar:
        registrar_atividade()
        preco_decimal = valor_decimal(preco)
        erro = validar_livro_formulario(titulo, autor, ano, preco_decimal)

        if erro:
            st.error(erro)
        else:
            payload = {
                "titulo": titulo.strip(),
                "autor": autor.strip(),
                "ano_publicacao": int(ano),
                "preco": format(preco_decimal, ".2f"),
                "estoque": int(estoque),
            }

            response = requisitar_api(
                "POST", "/livros", autenticada=True,
                verificar_status=False, json=payload,
            )

            resultado = validar_resposta_json(
                response,
                status_esperado=201,
                contexto="a confirmação do cadastro do livro",
                campos_obrigatorios=("mensagem", "livro"),
            )

            if resultado is not None:
                livro_resultado = resultado["livro"]
                if not isinstance(livro_resultado, dict) or not CAMPOS_LIVRO.issubset(livro_resultado):
                    st.error("A API confirmou o cadastro, mas retornou o livro incompleto.")
                else:
                    st.success(resultado["mensagem"])
                    st.session_state.geracao_tabela += 1
                    agendar_limpeza_edicao()
                    time.sleep(1)
                    st.rerun()

# AUDITORIA
if possui_scope("auditoria:read"):
    st.markdown("### 📋 Auditoria")
    col_limite, col_atualizar = st.columns([1, 4])

    with col_limite:
        limite = st.selectbox(
            "Registros",
            [25, 50, 100, 250],
            index=2,
            key="limite_auditoria",
            on_change=registrar_atividade,
        )

    with col_atualizar:
        st.write("")
        st.write("")
        st.button("🔄 Atualizar auditoria", key="atualizar_auditoria", on_click=registrar_atividade)

    response_auditoria = requisitar_api(
        "GET",
        "/auditoria",
        autenticada=True,
        verificar_status=False,
        params={"limite": limite},
    )

    logs = validar_resposta_json(
        response_auditoria,
        contexto="a lista de auditoria",
        tipo_esperado=list,
    )

    if logs is not None:
        campos_log = {
            "id", "usuario", "operacao", "recurso", "recurso_id",
            "data_hora", "resultado", "detalhes",
        }

        estrutura_valida = all(
            isinstance(log, dict) and campos_log.issubset(log)
            for log in logs
        )

        if not estrutura_valida:
            st.error("A API retornou um registro de auditoria incompleto ou inválido.")
        elif logs:
            try:
                df_logs = pd.DataFrame(logs)

                datas = pd.to_datetime(
                    df_logs["data_hora"],
                    utc=True,
                    errors="coerce",
                )

                if datas.isna().any():
                    raise ValueError("data inválida")

                df_logs["data_hora"] = datas.dt.strftime("%d/%m/%Y %H:%M:%S")
                df_logs = df_logs.rename(columns={
                    "id": "ID",
                    "usuario": "Usuário",
                    "operacao": "Operação",
                    "recurso": "Recurso",
                    "recurso_id": "ID Recurso",
                    "data_hora": "Data/Hora (UTC)",
                    "resultado": "Resultado",
                    "detalhes": "Detalhes",
                })

                df_logs = df_logs[[
                    "ID", "Data/Hora (UTC)", "Usuário", "Operação",
                    "Recurso", "ID Recurso", "Resultado", "Detalhes",
                ]]

                st.dataframe(df_logs, hide_index=True, width="stretch", height=300)
            except (TypeError, ValueError, KeyError):
                st.error("A API retornou datas ou dados inválidos na auditoria.")
        else:
            st.info("Ainda não existem registros de auditoria.")
