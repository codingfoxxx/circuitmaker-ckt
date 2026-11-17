# circuitmaker-ckt

Skill do [Claude Code](https://claude.com/claude-code) para gerar, ler, corrigir e simular esquemáticos do
**CircuitMaker 2000** (`.CKT`, o antigo da Protel/MicroCode, não o CircuitMaker atual da Altium) em
exercícios de Eletrônica Digital: contadores (7490, 7493, 7492, 74192/193, 74160-163), divisores de
frequência, registradores de deslocamento (74165/74164), portas 74xx/40xx, displays, chaves hexa e Pulser.

A ideia é entregar circuitos **provados na simulação**, não só desenhados. Cada arquivo passa por três etapas:
1. uma checagem estática das ligações;
2. um simulador lógico próprio, com atrasos aleatórios para pegar corridas;
3. a simulação digital do próprio CircuitMaker, controlado por script.

## Instalação: copie e cole no Claude Code

```
Instale a skill circuitmaker-ckt: clone https://github.com/codingfoxxx/circuitmaker-ckt em ~/.claude/skills/circuitmaker-ckt (no Windows: %USERPROFILE%\.claude\skills\circuitmaker-ckt; se a pasta já existir, faça git pull nela), rode "pip install pywin32 pillow" e depois "python scripts/check_setup.py" dentro dessa pasta. Me diga se ficou faltando algo.
```

Quando o `check_setup.py` disser `ready`, abra uma conversa nova no Claude Code e peça o circuito. Por exemplo:
- "Projete no CircuitMaker um contador de 00 a 59 com dois 7490, clock automático e displays, e verifique."
- "Faça um divisor de frequência por 6 com o 7493 e me mostre as formas de onda."
- "Esse arquivo C:\...\CIRCUITO.CKT não funciona no CircuitMaker, descobre por quê."

A skill entrega os `.CKT`, as telas do CircuitMaker e um `VALIDACAO.md`. Esse arquivo diz o que foi verificado
e o que não foi.

### Instalação manual (PowerShell)
```powershell
git clone https://github.com/codingfoxxx/circuitmaker-ckt "$env:USERPROFILE\.claude\skills\circuitmaker-ckt"
pip install pywin32 pillow
python "$env:USERPROFILE\.claude\skills\circuitmaker-ckt\scripts\check_setup.py"
```
Sem git: use **Code → Download ZIP** e extraia em `%USERPROFILE%\.claude\skills\circuitmaker-ckt`. O `SKILL.md`
precisa ficar direto dentro dessa pasta.

## Requisitos
- Windows com o CircuitMaker 2000 em `C:\Program Files\CircuitMaker 2000`. Se estiver em outro lugar,
  defina a variável de ambiente `CM2000_DIR`.
- Python 3 (testado no 3.14) com `pywin32` e `Pillow`.
- Claude Code, para usar como skill. Os scripts também rodam sozinhos.

## Uso direto dos scripts
Rode de dentro de `scripts/`:

```
python check_setup.py                                # confere Python, dependências e o CircuitMaker
python ckt.py inspect ARQ.CKT                        # netlist com o nome de cada pino
python validate.py ARQ.CKT                           # entradas soltas, saídas em conflito, fio sobre pino
python logicsim.py ARQ.CKT --clock CLK --pulses 50 --sweep 40   # simulação 0/1/X; "robust" = sem corrida
python devlib.py show 74LS90                         # pinos de um componente da biblioteca do CM
python cm_verify.py nets ARQ.CKT                     # abre no CircuitMaker e compara a netlist dele com a nossa
python cm_verify.py count ARQ.CKT --pulses 50 --displays DEZ,UNI   # lê os displays a cada pulso
python cm_verify.py wave ARQ.CKT ondas.png --ticks 600   # formas de onda das pontas SCOPE, em texto e PNG
```

Para gerar um circuito por código, use a API `Schematic` do `build.py`. O `SKILL.md` tem um exemplo mínimo.

## Estrutura
| pasta | conteúdo |
|---|---|
| `SKILL.md` | fluxo de trabalho e fatos do CircuitMaker que decidem se o circuito funciona |
| `references/` | formato do `.CKT`, automação da interface, receitas de projeto, tabela de pinos |
| `scripts/` | parser/escritor, construtor, validador, simulador, automação do CircuitMaker, diagnóstico |
| `assets/` | modelos de componentes tirados de arquivos salvos pelo CircuitMaker |
| `evals/` | casos de teste da skill |

## Limites
- O modo digital do CircuitMaker mede o tempo em ticks, não em segundos. O que a simulação prova é a lógica e
  as razões de frequência, não a frequência em Hz.
- `cmgui.py` e `cm_verify.py` assumem o controle da janela do CircuitMaker. Não use com um circuito aberto e
  não salvo.

## Licença
[MIT](LICENSE). O CircuitMaker é marca de seus respectivos donos. Este projeto não inclui o programa e não tem
vínculo com eles.
