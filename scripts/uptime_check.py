#!/usr/bin/env python3
"""Vigia dos endereços públicos do Augusto. Roda no GitHub Actions, de graça.

POR QUE EXISTE
O OpenStatus cobre um endereço — o plano gratuito dá exatamente um monitor, e ele
está no luckycat.ie, que é onde há caminho de dinheiro. Os outros cinco endereços
públicos não tinham vigilância nenhuma: se o portfólio caísse na véspera de uma
entrevista, ninguém saberia.

CUSTO
Zero, sem prazo. A documentação do GitHub é explícita: "GitHub Actions usage is free
for self-hosted runners and for public repositories that use standard GitHub-hosted
runners". Este repositório é público e o runner é o padrão (ubuntu-latest). A única
exceção da política são os "larger runners", que não são usados aqui.

O QUE ELE AUTOMATIZA (e o que não dá para automatizar)
Não existe script que religue o Cloudflare. O que dá — e é a maior parte do trabalho
chato — é tudo em volta:
  * confirmar antes de alarmar: 3 tentativas espaçadas, porque um soluço de rede não
    é uma queda e alarme falso treina a pessoa a ignorar alarme;
  * dizer o que quebrou: DNS, TLS, HTTP ou conteúdo — cada um pede uma ação diferente;
  * abrir UMA issue por serviço, com o diagnóstico e o próximo passo já escrito;
  * atualizar essa mesma issue enquanto durar, em vez de abrir uma por verificação;
  * FECHAR a issue sozinho quando o serviço voltar, dizendo quanto tempo ficou fora.
A issue vira notificação do GitHub no celular e no e-mail, sem configurar nada.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "augbastos/.github")
TENTATIVAS = 3
ESPERA = 20          # segundos entre tentativas
TIMEOUT = 25
LABEL = "uptime"

# `espera` é a substring que prova que a página é a página, e não uma tela de erro
# do provedor com status 200. Sem isso, um "site no ar" pode ser a página de manutenção
# do Cloudflare, que responde 200 alegremente.
ALVOS = [
    {"nome": "Portfolio & CV", "url": "https://augustobastos.pages.dev/", "espera": None,
     "porque": "é o que um recrutador abre"},
    {"nome": "devcard (SVG ao vivo)", "url": "https://card.devcard.workers.dev/svg?user=augbastos",
     "espera": "<svg", "porque": "é o card no topo do README de perfil; se cai, o perfil mostra imagem quebrada"},
    {"nome": "Tillr (pedido do cliente)", "url": "https://luckycat.ie/template/tillr/", "espera": None,
     "porque": "é o caminho de dinheiro do Lucky Cat"},
    {"nome": "Ownly (painel do dono)", "url": "https://luckycat.ie/template/ownly/", "espera": None,
     "porque": "é o painel que um restaurante usaria no dia a dia"},
    {"nome": "SCPE (página do protocolo)", "url": "https://augbastos.github.io/scpe/", "espera": "SCPE",
     "porque": "está linkada no README, no PyPI e na página do projeto"},
    {"nome": "Lucky Cat (site)", "url": "https://luckycat.ie/", "espera": None,
     "porque": "é o apex; o OpenStatus também cobre, daqui serve de segunda opinião"},
]

ACAO = {
    "dns": "O nome não resolve. Verifique o DNS na Cloudflare — para luckycat.ie, se o registro do apex sumiu ou expirou.",
    "tls": "O certificado falhou. Em domínio na Cloudflare costuma se resolver sozinho; se persistir, veja SSL/TLS > Edge Certificates.",
    "conexao": "Não completou a conexão. Provedor fora do ar ou bloqueio de rede — confira o painel de status do provedor antes de mexer em qualquer coisa.",
    "http": "Respondeu, mas com erro. 5xx é o provedor; 404 costuma ser deploy que publicou a árvore errada; 403 costuma ser regra de acesso.",
    "conteudo": "Respondeu 200 mas sem o conteúdo esperado — normalmente é a página de erro do provedor devolvida com status 200, ou um deploy que subiu vazio.",
}


def agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def checar(alvo: dict) -> dict:
    """Uma tentativa. Devolve o que aconteceu, classificado por camada."""
    inicio = time.time()
    req = urllib.request.Request(alvo["url"], headers={"User-Agent": "augbastos-uptime/1 (+github actions)"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            corpo = r.read(60000).decode("utf-8", "replace")
            ms = int((time.time() - inicio) * 1000)
            if alvo.get("espera") and alvo["espera"] not in corpo:
                return {"ok": False, "tipo": "conteudo", "ms": ms,
                        "detalhe": f'HTTP {r.status} mas sem "{alvo["espera"]}" no corpo'}
            return {"ok": True, "tipo": None, "ms": ms, "detalhe": f"HTTP {r.status} em {ms} ms"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "tipo": "http", "ms": int((time.time() - inicio) * 1000),
                "detalhe": f"HTTP {e.code} {e.reason}"}
    except ssl.SSLError as e:
        return {"ok": False, "tipo": "tls", "ms": 0, "detalhe": f"TLS: {e}"}
    except urllib.error.URLError as e:
        motivo = e.reason
        tipo = "dns" if isinstance(motivo, socket.gaierror) else ("tls" if isinstance(motivo, ssl.SSLError) else "conexao")
        return {"ok": False, "tipo": tipo, "ms": 0, "detalhe": f"{type(motivo).__name__}: {motivo}"}
    except Exception as e:  # noqa: BLE001 - qualquer coisa aqui é queda, não crash do vigia
        return {"ok": False, "tipo": "conexao", "ms": 0, "detalhe": f"{type(e).__name__}: {e}"}


def confirmar(alvo: dict) -> dict:
    """Só chama de queda o que falhou nas TENTATIVAS vezes. Um soluço não é uma queda."""
    ultima = {}
    for i in range(TENTATIVAS):
        ultima = checar(alvo)
        if ultima["ok"]:
            return {**ultima, "tentativas": i + 1}
        if i < TENTATIVAS - 1:
            time.sleep(ESPERA)
    return {**ultima, "tentativas": TENTATIVAS}


def gh(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or r.stderr or "").strip()


def issue_aberta(nome: str) -> dict | None:
    c, out = gh(["issue", "list", "--repo", REPO, "--label", LABEL, "--state", "open",
                 "--json", "number,title,body,createdAt", "--limit", "50"])
    if c != 0:
        return None
    for i in json.loads(out or "[]"):
        if i["title"].startswith(f"🔴 {nome}"):
            return i
    return None


def main() -> int:
    caidos, ok = [], []
    for alvo in ALVOS:
        r = confirmar(alvo)
        marca = "ok  " if r["ok"] else "FORA"
        print(f"  {marca} {alvo['nome']:<28} {r['detalhe']}")
        (ok if r["ok"] else caidos).append((alvo, r))

    for alvo, r in caidos:
        existente = issue_aberta(alvo["nome"])
        acao = ACAO.get(r["tipo"], "")
        corpo = (
            f"**{alvo['url']}** não respondeu como devia.\n\n"
            f"| | |\n|---|---|\n"
            f"| Falhou em | {agora()} |\n"
            f"| O que aconteceu | {r['detalhe']} |\n"
            f"| Camada | {r['tipo']} |\n"
            f"| Tentativas | {r['tentativas']} (espaçadas {ESPERA}s — não é soluço de rede) |\n"
            f"| Por que importa | {alvo['porque']} |\n\n"
            f"**Próximo passo:** {acao}\n\n"
            f"<sub>Esta issue se fecha sozinha quando o endereço voltar. Vigia: "
            f"`.github/workflows/uptime.yml`.</sub>"
        )
        if existente:
            gh(["issue", "comment", str(existente["number"]), "--repo", REPO,
                "--body", f"Ainda fora em {agora()} — {r['detalhe']}"])
            print(f"  ! issue #{existente['number']} atualizada ({alvo['nome']})")
        else:
            c, out = gh(["issue", "create", "--repo", REPO, "--label", LABEL,
                         "--title", f"🔴 {alvo['nome']} está fora do ar", "--body", corpo])
            print(f"  ! issue aberta ({alvo['nome']}): {out.splitlines()[-1] if c == 0 else out[:120]}")

    # A recuperação também é automática: o que voltou fecha a própria issue.
    for alvo, r in ok:
        existente = issue_aberta(alvo["nome"])
        if existente:
            desde = existente["createdAt"][:16].replace("T", " ")
            gh(["issue", "close", str(existente["number"]), "--repo", REPO, "--comment",
                f"✅ Voltou em {agora()} — {r['detalhe']}. Ficou fora desde {desde} UTC."])
            print(f"  + issue #{existente['number']} fechada ({alvo['nome']} voltou)")

    print(f"\n{len(ok)}/{len(ALVOS)} no ar.")
    # Sai 0 mesmo com queda: a queda vira issue, não um workflow vermelho todo dia.
    # Um vigia que fica vermelho é um vigia que a pessoa aprende a ignorar.
    return 0


if __name__ == "__main__":
    sys.exit(main())
