<h1 align="center">Katomart</h1>

<p align="center"><strong>Downloader de cursos EAD que virou um aplicativo completo de estudos.</strong><br>Baixe os cursos da sua conta, assista, faça flashcards, questões e cadernos, tudo offline, sem instalar Python nem configurar nada.</p>

<div align="center">
  <a href="https://katomaro.com/store/katomart" target="_blank"><img alt="Baixar o Katomart" src="https://img.shields.io/badge/Baixar%20o%20Katomart-katomaro.com-7c3aed?style=for-the-badge"></a>
  <a href="https://apps.microsoft.com/detail/XP9LZZLQ6V0SKM?hl=pt-BR&gl=BR&ocid=github" target="_blank"><img alt="Microsoft Store" src="https://img.shields.io/badge/Microsoft%20Store-Instalar-0078D4?style=for-the-badge&logo=microsoft"></a>
  <a href="https://t.me/katomart" target="_blank"><img alt="Telegram" src="https://img.shields.io/badge/Telegram-Entrar-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white"></a>
</div>

<br>

> [!IMPORTANT]
> **Este repositório virou uma placa de sinalização.** O código-fonte em Python que vivia aqui foi removido e não recebe mais suporte. O Katomart continua existindo, cresceu bastante, e agora é distribuído pronto para instalar em [katomaro.com/store/katomart](https://katomaro.com/store/katomart) e na [Microsoft Store](https://apps.microsoft.com/detail/XP9LZZLQ6V0SKM?hl=pt-BR&gl=BR&ocid=github).
>
> Por favor, não abra issues nem envie e-mails pedindo atualização deste código. Toda a energia foi para o aplicativo novo.

## O que mudou

Este repositório nasceu como um projeto de estudos: um script para fazer backup pessoal dos cursos aos quais a sua própria conta tem acesso, em plataformas como Hotmart, Udemy, Kiwify, Eduzz, Estratégia Concursos e Gran Cursos. Para rodar, você precisava instalar o Python 3.12, criar um ambiente virtual, instalar dependências, baixar o Chromium do Playwright, apontar o FFmpeg e o Bento4 no PATH e torcer para nada quebrar no caminho.

O Katomart de hoje é outro produto:

| Antes (este repositório) | Agora (katomaro.com/store/katomart) |
|---|---|
| Script em Python, rodava pelo terminal | Aplicativo instalável para Windows, Linux e macOS |
| Instalar Python, venv, pip, Playwright, FFmpeg, Bento4 | Baixar e executar. Nada de dependência manual |
| Só baixava | Baixa, organiza, reproduz e ajuda a estudar |
| Token e cookie extraídos na mão | Login com e-mail e senha, sessão renovada sozinha |
| Atualização por `git pull` | Atualização automática pela Microsoft Store, instalador assinado |
| Estudava decriptografia de mídia | Mídia com proteção técnica é detectada e não é baixada |
| Termos em um arquivo Markdown | Termos versionados, com histórico de aceite, em [katomaro.com/legal](https://katomaro.com/legal/) |

O motor de download deste repositório foi reescrito e virou o módulo **Aquisição** do aplicativo. O restante do app foi construído em volta dele.

## Baixar o Katomart

- **Windows:** [instale pela Microsoft Store](https://apps.microsoft.com/detail/XP9LZZLQ6V0SKM?hl=pt-BR&gl=BR&ocid=github) para ter instalador assinado e atualização automática, ou baixe o instalador direto em [katomaro.com/store/katomart](https://katomaro.com/store/katomart).
- **Linux:** AppImage disponível em [katomaro.com/store/katomart](https://katomaro.com/store/katomart).
- **macOS:** instalador disponível em [katomaro.com/store/katomart](https://katomaro.com/store/katomart).
- **Android e iOS:** em desenvolvimento, sem data anunciada.

Não é preciso instalar Python, FFmpeg, Playwright ou qualquer outra ferramenta. Baixe, abra e use. Katomart é marca registrada no INPI (processo nº 935836349).

## O que é grátis e o que é pago

O núcleo do aplicativo é gratuito para sempre: importar cursos de pastas locais, assistir com o player, marcar aulas concluídas, anotar por marca de tempo, buscar em tudo o que você já tem e usar o essencial de flashcards, questões, cadernos, mapas mentais e planner. A biblioteca gratuita comporta 3 cursos e 80 aulas, o suficiente para testar se o conteúdo da sua conta é baixável antes de pagar qualquer coisa. Qualquer módulo pago já tira esse limite.

Você assina só o que usa. São quatro módulos:

| Módulo | Para quem quer | O que libera |
|---|---|---|
| **Aquisição** | Baixar | Login nas plataformas com e-mail e senha, sessão renovada sozinha, downloads em paralelo, todas as configurações de download, repetir falhas, agendamento de aulas novas, FFmpeg e transcrição no download, e arquivo de plataforma corrigido sem esperar versão nova do app. Inclui a integração com o Katomart IA: dublagem e narração sem limite, e a opção de transcrever e dublar o que você baixar |
| **Nuvem** | Guardar | Backups dos cursos no seu Telegram ou na sua nuvem via rclone, e reprodução direto de lá pela aba Cursos (Stream), sem baixar tudo de novo |
| **Estudo Ativo** | Memorizar e treinar | Flashcards sem limite, com imagem, áudio e repetição espaçada completa. Bancos de questões e simulados sem limite, com estatísticas avançadas e operações em lote. Cadernos e mapas mentais sem limite |
| **Inteligência** | Planejar | Aba de IAs com a sua própria chave, Planner sem limite de tarefas e transcrição de áudio sem limite diário. Inclui a integração com o Katomart IA: dublagem e narração sem limite, rodando no seu computador |

Qual é o seu caso?

- **Só quero baixar meus cursos:** Aquisição.
- **Baixar e guardar na nuvem:** Aquisição + Nuvem, o pacote Arquivista.
- **Já tenho os cursos, quero estudar direito:** Estudo Ativo + Inteligência, o pacote Estudante.
- **Quero tudo:** pacote Completo ou licença vitalícia. Só esses dois liberam o premium inteiro, incluindo a sincronização do Planner com o site e o Google Calendar.

Nenhum módulo faz baixar conteúdo com proteção técnica, e nenhum dá direito de repassar material.

### Preços de tabela

Vale sempre o valor que aparece em [katomaro.com/store/katomart](https://katomaro.com/store/katomart) na hora da compra. Em promoção, o site mostra o preço cheio riscado. Tabela de 22/09/2026, por mês:

| Plano | Composição | Preço |
|---|---|---|
| Aquisição | módulo avulso | R$ 12,90 |
| Nuvem | módulo avulso | R$ 9,90 |
| Estudo Ativo | módulo avulso | R$ 14,90 |
| Inteligência | módulo avulso | R$ 7,90 |
| **Arquivista** | Aquisição + Nuvem | R$ 17,90 |
| **Estudante** | Estudo Ativo + Inteligência | R$ 19,90 |
| **Completo** | os 4 módulos, premium inteiro | R$ 29,90 |

Os pacotes saem sempre mais baratos que somar os módulos, e o site já junta sozinho quando você marca os módulos. Assinando por período o desconto é de 5% no trimestral, 10% no semestral, e no anual você paga 10 meses e leva 12. Quem assina com desconto fica naquele preço até cancelar.

Add-ons opcionais:

- **Servidor de Mídia:** R$ 7,90 com 3 contas inclusas, mais R$ 2,00 por conta extra. Transforma o seu computador no servidor de estudos da casa, com contas e permissões por curso. Disponível para quem tem Aquisição, Nuvem ou Completo.
- **Colaboração (co-work):** R$ 5,00. A colaboração básica entre Katomarts já é grátis. O add-on libera ao seu convidado, só dentro da sessão, os módulos que você tem e ele não.

A **licença vitalícia** libera todos os módulos com pagamento único, sem mensalidade. O preço sobe conforme o app evolui, e quem já comprou não é afetado. Detalhes na [página do produto](https://katomaro.com/store/katomart).

Regras que valem para todos: venda apenas para pessoa física (CPF), 7 dias de arrependimento, cancelamento a qualquer momento no perfil, sem reembolso proporcional. Se você já pagava a assinatura do Katomart antigo, você já é Arquivista no app novo, com o preço antigo congelado até cancelar.

Dúvidas sobre planos, limites, pagamento e reembolso: comando `/help` no bot [@katomaro_bot](https://t.me/katomaro_bot) no Telegram.

## Plataformas suportadas

O Katomart baixa, com o seu próprio login, o conteúdo ao qual a sua conta tem acesso legítimo em **114 plataformas** de ensino a distância. A referência canônica, com status em tempo real, fica em [katomaro.com/platforms](https://katomaro.com/platforms). A mesma lista é servida pelo bot [@katomaro_bot](https://t.me/katomaro_bot) no Telegram pelo comando `/platformas`. Não sabe em qual plataforma a sua área de membros está hospedada? Use o comando `/localizar` no mesmo bot.

Plataformas whitelabel permitem que o produtor esconda o nome e a aparência originais atrás do próprio domínio. Você pode estar usando uma sem saber. Integração experimental significa cobertura parcial e sujeita a mudanças.

Lista capturada em 22/09/2026:

| Plataforma | Proteção técnica em parte do conteúdo | Whitelabel | Observação |
|---|---|---|---|
| AWS Treina Brasil | Não | Não |  |
| Academia Rafael Toro | Não | Não |  |
| Academia da Tributação | Não | Não | Integração experimental |
| Afya Internato | Não | Não |  |
| AlfaCon | Não | Não |  |
| AlpaClass Domínio Customizado | Não | Sim |  |
| Alura | Não | Não |  |
| Asimov Academy | Não | Não |  |
| Assaad | Não | Não |  |
| Astronmembers | Não | Sim |  |
| AvMakers | Não | Não |  |
| B7School | Não | Não | Integração experimental |
| BJJ Protech | Não | Não | Integração experimental |
| Brasil Paralelo | Sim | Não | Integração experimental. Aulas protegidas por lei não são baixadas |
| CP Iuris | Não | Não |  |
| Cademi | Não | Sim |  |
| Cakto | Não | Não |  |
| Cakto (Variante Members) | Não | Sim |  |
| Circle.so | Não | Sim |  |
| Classplay (multi-plataforma) | Não | Sim | Integração experimental |
| Cod3r | Não | Não |  |
| Contabilidade Facilitada | Não | Não |  |
| Coursera | Não | Não |  |
| Curseduca | Sim | Sim | Aulas protegidas por lei não são baixadas |
| Curso PM3 | Não | Não |  |
| Cursos do Portal (Portal Concursos) | Não | Não |  |
| CyberClass | Não | Não |  |
| Danki Code | Não | Não |  |
| DascIA Academy | Não | Não |  |
| Data Science Academy | Não | Não |  |
| Decorando a Lei Seca | Não | Não | Integração experimental |
| Dedicação Delta / Aprovação PGE | Não | Não | Integração experimental |
| Design Boost | Não | Não |  |
| Direção Concursos | Não | Não |  |
| EAD Plataforma | Sim | Sim | Aulas protegidas por lei não são baixadas |
| Eduzz/Nutror | Não | Sim |  |
| Elevify | Não | Não |  |
| Entrega Digital | Não | Não |  |
| Estacio | Não | Não |  |
| Estratégia Concursos | Não | Não |  |
| Estratégia Concursos (LDI) | Não | Não |  |
| Estratégia Medicina | Não | Não |  |
| Estratégia Militares | Não | Não |  |
| Expert em TI | Não | Não |  |
| Faculdade Trader | Não | Não | Integração experimental |
| Fiap ON | Não | Não |  |
| FinClass | Não | Não |  |
| Fluency Academy | Não | Não |  |
| Focus Concursos | Não | Não |  |
| Full Cycle | Sim | Não | Aulas protegidas por lei não são baixadas |
| G7 Jurídico | Não | Não | Integração experimental |
| GB Digital (Criar & Vender) | Não | Não | Integração experimental |
| GoExplosion (áreas de membros) | Não | Sim | Integração experimental |
| Gramatique Concursos | Não | Não | Integração experimental |
| Gran Cursos Online | Não | Não |  |
| Grasiela Cabral | Não | Não | Integração experimental |
| Greenn Club | Não | Sim |  |
| Grupo Educacional RDP (Defensoria / MP / Magistratura / Policial) | Não | Não | Integração experimental |
| Grupo GEN | Não | Não |  |
| Gustavo Kalau | Não | Não |  |
| HackOne | Não | Não |  |
| Hashtag Treinamentos | Não | Sim | Integração experimental |
| HeroSpark | Não | Sim | Integração experimental |
| Hotmart | Sim | Não | Aulas protegidas por lei não são baixadas |
| Hub.la | Não | Sim |  |
| Human Academy | Não | Não |  |
| INE (my.ine.com) | Não | Não |  |
| Instituto Setor Elétrico | Não | Não | Integração experimental |
| Kajabi | Não | Sim |  |
| Kirvano | Não | Sim |  |
| Kiwify | Não | Não |  |
| Lastlink | Não | Não |  |
| MIOS Academy | Não | Não | Integração experimental |
| Magistra Educacional | Não | Não | Integração experimental |
| Mairo Vergara | Não | Não | Integração experimental |
| Matemática Pra Passar | Não | Não |  |
| Medcel | Não | Não |  |
| Medcof | Não | Não |  |
| Medway | Não | Não |  |
| Mege | Não | Não | Integração experimental |
| MemberKit | Não | Sim |  |
| My Farm Agro | Não | Não |  |
| NEAF Concursos | Não | Não |  |
| O Novo Mercado | Não | Não |  |
| Origamid | Não | Não |  |
| PGATS - Julio de Lima | Não | Não |  |
| Padre Paulo Ricardo | Não | Não |  |
| Plataforma do Cenoura | Não | Não |  |
| Plato Edu | Não | Não | Integração experimental |
| Prof Kenny | Não | Não |  |
| Professor Ferretto | Não | Não |  |
| Projeto Caveira | Não | Não |  |
| Projeto Missão | Não | Não |  |
| Quebrando as Bancas | Não | Não | Integração experimental |
| Rocketseat Journeys | Não | Não |  |
| Skool | Não | Não |  |
| StreamMembers | Não | Não |  |
| Subido | Não | Não | Integração experimental |
| Supremo TV | Não | Não | Integração experimental |
| TPD Play | Não | Não | Integração experimental |
| Teachable | Não | Não |  |
| TheMembers | Não | Sim |  |
| Thinkr (Legale e outras escolas) | Não | Sim | Integração experimental |
| Ticto (Mozart EAD) | Não | Não |  |
| TreinaWeb | Não | Não | Integração experimental |
| Tutory | Não | Não |  |
| Udemy | Sim | Não | Aulas protegidas por lei não são baixadas |
| Unhide School | Não | Não |  |
| Voomp Play | Não | Sim |  |
| XP Educação | Não | Não |  |
| Xperiun | Não | Não |  |
| branas.io | Não | Não |  |
| curso.dev | Não | Não |  |
| Área de Membros | Não | Sim |  |

Quando uma plataforma é corrigida ou implementada, o arquivo compilado dela é publicado em [katomaro.com/platforms](https://katomaro.com/platforms) antes da próxima versão do app, e o Katomart instala pela aba Downloaders. Se a sua plataforma não aparece, gere um arquivo HAR (comando `/harfile` no bot) e envie para [@katomaro](https://t.me/katomaro).

## Sobre conteúdo protegido e decriptografia

Uma parte deste repositório era dedicada a estudar como funcionava a decriptografia de mídia (Widevine, CDM, Bento4). Esse estudo foi encerrado junto com este código e não foi levado para o aplicativo. O Katomart atual funciona com o seu próprio login, para uso pessoal, e trata mídia com proteção técnica de um jeito só: detecta e não baixa. Alguns conteúdos podem não ser baixáveis por lei, e o app respeita isso.

A base legal de uso, a política de notificações e a responsabilidade do usuário final que antes viviam no `LEGAL.md` deste repositório agora são documentos versionados, com registro de aceite, em [katomaro.com/legal](https://katomaro.com/legal/). Cada alteração gera uma nova versão e, quando aplicável, um novo pedido de consentimento. É lá que valem os termos, não em cópias antigas ou forks deste repositório.

## O que continua aberto

O Katomart não abandonou o código aberto. Ele mudou de lugar:

- **[Katomart IA](https://github.com/katomaro/Katomart-ia)**, sob licença Apache-2.0: transcrição com Whisper, tradução offline e dublagem com vozes prontas, tudo rodando no seu computador, com uma API local que o Katomart detecta sozinho. Quem quer ver código, contribuir ou rodar do fonte, é por lá.
- **Curso de programação gratuito**, em produção: um curso que compila todo o estudo por trás do Katomart, exceto decriptografia, para você aprender a construir automações, downloaders e aplicativos desktop do zero. Será disponibilizado de graça em [katomaro.com](https://katomaro.com). Avisos de lançamento no [Telegram](https://t.me/katomart).

## Perguntas frequentes

**Eu clonei este repositório antes. Ainda funciona?**
Pode até funcionar por um tempo, mas as plataformas mudam toda semana e nada aqui será corrigido. Cada hora gasta tentando manter o script vivo é uma hora que o aplicativo novo já resolveu.

**Eu uso Linux ou macOS. Preciso do código-fonte?**
Não. Existem instaladores para os dois sistemas em [katomaro.com/store/katomart](https://katomaro.com/store/katomart).

**Meus cursos e dados ficam onde?**
No seu computador. O Katomart é local por padrão. A nuvem, quando você usa, é sempre a sua conta de Telegram ou o seu remoto rclone.

**Posso compartilhar o que baixei?**
Não. O download é para uso pessoal, com a sua própria conta. O compartilhamento é proibido pela licença de uso, e o autor é totalmente contrário a isso. Se encontrar alguém redistribuindo material, não confunda as identidades.

**Onde tiro dúvidas?**
No [grupo do Telegram](https://t.me/katomart) ou pelos canais de suporte do site.

---

<details>
<summary><strong>English summary</strong></summary>

**Katomart** started as an open-source Python course downloader for e-learning platforms (Hotmart, Udemy, Kiwify, Eduzz, Coursera, Teachable, Kajabi, Estratégia Concursos, Gran Cursos and over 114 platforms in total, listed at [katomaro.com/platforms](https://katomaro.com/platforms)). The source code that used to live here has been removed and is no longer maintained.

The project became a full study application, distributed as a ready-to-install app for Windows, Linux and macOS. No Python, no virtual environment, no manual dependencies: download and run.

- Download: [katomaro.com/store/katomart](https://katomaro.com/store/katomart)
- Windows: [Microsoft Store](https://apps.microsoft.com/detail/XP9LZZLQ6V0SKM?hl=en-US&gl=BR&ocid=github)
- Legal terms, versioned: [katomaro.com/legal](https://katomaro.com/legal/)
- Still open source: [Katomart IA](https://github.com/katomaro/Katomart-ia) (Apache-2.0), local Whisper transcription, translation and dubbing.

The core of the app is free forever. Paid modules remove limits and unlock advanced features. Media with technical protection is detected and not downloaded; the decryption research that used to live in this repository was not carried over to the app.

</details>
