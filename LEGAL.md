# Políticas Legais — Katomart

**Última atualização:** 7 de maio de 2026
**Mantenedor:** Victor Hugo Carvalho dos Reis Santos
**Contato oficial:** victorhugoc.dosreissantos@gmail.com

---

## Índice

1. [Natureza do Projeto](#1-natureza-do-projeto)
2. [Política de Notificações Extrajudiciais (DMCA / Takedown)](#2-política-de-notificações-extrajudiciais-dmca--takedown)
3. [Tratativas Técnicas com Plataformas](#3-tratativas-técnicas-com-plataformas)
4. [Fundamentação Legal de Uso e Responsabilidade do Usuário Final](#4-fundamentação-legal-de-uso-e-responsabilidade-do-usuário-final)

---

## 1. Natureza do Projeto

O **Katomart** é um software de código aberto (open-source), distribuído sob licença pública, que opera estritamente como **cliente local de automação**. Suas características arquiteturais fundamentais são:

- **Execução exclusivamente local:** todo processamento ocorre na máquina do usuário final.
- **Autenticação por credenciais do próprio usuário:** o software não possui, não armazena e não distribui credenciais. O acesso a qualquer plataforma suportada depende integralmente de credenciais legítimas pertencentes ao usuário.
- **Ausência de hospedagem ou redistribuição:** o Katomart não hospeda, transmite, intermedia tráfego ou redistribui conteúdo de plataformas terceiras. Conteúdo eventualmente processado permanece na máquina local do usuário.
- **Suporte multiplataforma:** o projeto suporta dezenas de plataformas distintas, configurando ferramenta de uso geral, não direcionada a nenhuma plataforma ou caso de uso específico.

A operação técnica do Katomart é funcionalmente equivalente à de um navegador web autenticado, diferenciando-se apenas pela automação de etapas operacionais.

---

## 2. Política de Notificações Extrajudiciais (DMCA / Takedown)

### 2.1. Fundamento Legal

Em conformidade com o **art. 19 da Lei nº 12.965/2014 (Marco Civil da Internet)**, a remoção de conteúdo, código ou funcionalidade do repositório oficial somente será efetuada mediante **ordem judicial específica**, salvo nas hipóteses excepcionais expressamente previstas em lei ou nas hipóteses de responsabilização ampliada estabelecidas pelo Supremo Tribunal Federal no julgamento do Tema 987 (RE 1037396).

Notificações extrajudiciais não criam, por si só, obrigação de remoção, mas serão analisadas com seriedade quando devidamente fundamentadas e apresentadas pela via correta.

### 2.2. Requisitos Formais para Notificações

Para que uma notificação seja processada, a solicitação deve atender aos seguintes requisitos cumulativos:

**a) Origem verificável:**
- Comunicação a partir de **domínio corporativo** verificável da plataforma ou entidade detentora dos direitos alegados; **ou**
- Apresentação de **certificado digital válido** ou **assinatura PGP** vinculados à entidade representada; **ou**
- Apresentação de **procuração formal específica** com poderes expressos para representação extrajudicial neste caso, no caso de notificações originadas de intermediários.

**b) Fundamentação jurídica específica:**
- Indicação precisa do dispositivo legal alegadamente violado;
- Descrição técnica e específica da suposta violação, com indicação exata do trecho de código, arquivo ou funcionalidade objeto da notificação;
- Apresentação de evidência técnica reproduzível, quando aplicável.

**c) Identificação completa do solicitante:**
- Nome completo do representante legal;
- Cargo e vínculo com a entidade representada;
- Dados de contato direto (não exclusivamente via intermediário).

### 2.3. Notificações Originadas por Intermediários

Notificações originadas por **empresas de proteção de marca, monitoramento de conteúdo, gestão de direitos digitais ou intermediários terceirizados** (incluindo, mas não limitado a, empresas como Axur, MarkMonitor, Incopro, Red Points e similares) somente serão processadas mediante:

1. Apresentação de procuração formal específica para o caso, em nome da entidade detentora dos direitos alegados, com poderes expressos para representação extrajudicial; **e**
2. Possibilidade de validação direta junto à entidade representada via canal corporativo verificável.

Notificações que não atendam a esses requisitos serão arquivadas sem processamento adicional.

### 2.4. Hipóteses Não Cobertas por Esta Política

As seguintes alegações **não constituem fundamento válido** para remoção de código ou funcionalidade do repositório oficial:

- **Citação de marca registrada** em caráter informativo, técnico ou de identificação de compatibilidade sistêmica, conforme amparado pelo art. 132, IV da Lei nº 9.279/96 (Lei de Propriedade Industrial).
- **Documentação técnica de endpoints** publicamente observáveis por usuários autenticados via ferramentas padrão de desenvolvedor, não constituindo segredo de empresa nem dado pessoal protegido pela Lei nº 13.709/2018 (LGPD).
- **Possibilidade abstrata de uso indevido** por terceiros, em ausência de elementos que caracterizem a ferramenta como direcionada a finalidade ilícita ou desprovida de utilidade legítima substancial.
- **Alegação genérica de "facilitação"** desacompanhada de demonstração técnica e jurídica de violação concreta atribuível ao mantenedor do projeto.

### 2.5. Transparência e Publicação

Notificações recebidas poderão ser publicadas integralmente neste repositório, com redação de dados pessoais sensíveis quando aplicável, em consonância com práticas estabelecidas por projetos open-source equivalentes (a exemplo do tratamento dado pelo GitHub a contranotificações DMCA).

A publicação tem por finalidade transparência comunitária e construção de precedente público sobre o tratamento de tais solicitações.

### 2.6. Canal Oficial

Notificações devem ser enviadas exclusivamente para:

**victorhugoc.dosreissantos@gmail.com**

(o mesmo endereço público utilizado em commits e nos perfis oficiais do mantenedor)

---

## 3. Tratativas Técnicas com Plataformas

Independentemente da posição jurídica acima, o projeto **mantém abertura para tratativas técnicas legítimas** com plataformas que demonstrem interesse em colaboração construtiva. Solicitações técnicas fundamentadas serão avaliadas ativamente, incluindo:

### 3.1. Ajustes de Requisições

Inclusão, remoção ou adequação de chamadas para endpoints específicos (por exemplo: endpoints de analytics, telemetria interna, ou rotas administrativas que não devam ser alcançadas por automação cliente).

**Requisitos:** apresentação técnica do impacto, exemplo do payload e justificativa.

### 3.2. Controle de Tráfego (Rate Limiting)

Implementação de limites de velocidade máxima (throttling) no comportamento de scraping da plataforma solicitante, mediante demonstração de que o volume de requisições oriundo de instâncias do Katomart está gerando anomalias comprovadas na infraestrutura.

**Requisitos:**
- Provas técnicas reproduzíveis;
- Identificação clara do padrão discriminador do Katomart no tráfego (User-Agent, headers, padrão de requisição);
- Demonstração de que a anomalia é atribuível ao Katomart e não a comportamento agregado de usuários legítimos da plataforma.

### 3.3. Integridade de Dados

O Katomart preserva o conteúdo recebido pela plataforma na forma em que é entregue. **Metadados ocultos, marcas d'água digitais ou mecanismos de rastreamento embutidos no fluxo de entrega (stream) pela plataforma permanecem intactos** no arquivo local gerado pelo usuário, sem qualquer interferência ou tentativa de remoção por parte do software.

### 3.4. Transparência de Alterações

**Toda alteração aceita será pública neste repositório.** O Katomart não implementa mecanismos de ofuscação. Usuários com conhecimento técnico mantêm a liberdade de criar forks ou compilar versões próprias sem restrições acordadas — a manutenção de tais restrições no repositório oficial será tratada com total transparência comunitária.

### 3.5. Pull Requests

Pull Requests técnicos que documentem ajustes de compatibilidade serão avaliados pelos critérios usuais de qualidade técnica do projeto, independentemente da origem corporativa ou individual do contribuidor.

### 3.6. Canal para Tratativas Técnicas

Mesmo endereço da seção 2.6:

**victorhugoc.dosreissantos@gmail.com**

Tratativas técnicas devem partir, preferencialmente, de membros da equipe de engenharia da plataforma, com identificação verificável.

---

## 4. Fundamentação Legal de Uso e Responsabilidade do Usuário Final

O funcionamento do Katomart é amparado pela legislação brasileira, país de origem do projeto. A operação do software encontra fundamento legal específico no **art. 184, § 4º do Código Penal Brasileiro**, que estabelece as exceções aos crimes contra os direitos autorais:

> **Art. 184. Violar direitos de autor e os que lhe são conexos:**
> Pena – detenção, de 3 (três) meses a 1 (um) ano, ou multa.
> [...]
> **§ 4º** O disposto nos §§ 1º, 2º e 3º não se aplica quando se tratar de exceção ou limitação ao direito de autor ou os que lhe são conexos, em conformidade com o previsto na Lei nº 9.610, de 19 de fevereiro de 1998, nem a cópia de obra intelectual ou fonograma, em um só exemplar, para uso privado do copista, sem intuito de lucro direto ou indireto.

### 4.1. Direito à Cópia Privada

A legislação federal brasileira reconhece expressamente o direito do titular de acesso legítimo a um conteúdo de produzir **cópia única para uso privado**, desde que ausente intuito de lucro direto ou indireto.

Em termos práticos: o usuário que detenha **acesso autenticado e legítimo** a material disponibilizado em plataforma terceira está amparado pela legislação federal para realizar cópia local destinada a uso estritamente privado.

**Termos de Uso (ToS) de plataformas ou infoprodutores não possuem força jurídica suficiente para anular garantia legal prevista em diploma federal.** A relação contratual entre plataforma e usuário não pode dispor, em sentido restritivo, sobre direitos assegurados em lei.

### 4.2. Responsabilidade Exclusiva do Usuário Final

O Katomart é **ferramenta neutra de automação**. A operação do software depende integralmente de execução pelo usuário final, mediante credenciais próprias. Recaem **exclusivamente** sobre o usuário, sem qualquer responsabilidade subsidiária ou solidária do mantenedor do projeto:

- A legitimidade das credenciais utilizadas;
- A conformidade do uso com os termos contratuais aceitos pelo usuário em cada plataforma;
- O enquadramento da cópia produzida nas hipóteses legais de uso privado;
- A destinação final do conteúdo eventualmente baixado.

O mantenedor do projeto não possui visibilidade, controle ou capacidade técnica de auditoria sobre instâncias do software executadas por terceiros.

### 4.3. Condutas Repudiadas

Embora o Katomart seja ferramenta neutra e seu mantenedor não exerça controle sobre uso individual, este projeto **repudia expressamente** as seguintes condutas, que extrapolam o amparo legal da cópia privada e configuram ilícitos autônomos:

#### 4.3.1. Abuso do Direito de Arrependimento

A prática de adquirir conteúdo em plataforma, realizar download integral via Katomart e, em seguida, requerer reembolso amparado no **art. 49 do Código de Defesa do Consumidor** (direito de arrependimento), com a finalidade exclusiva de obter o conteúdo sem contraprestação financeira efetiva, configura:

- **Abuso de direito**, nos termos do art. 187 do Código Civil ("também comete ato ilícito o titular de um direito que, ao exercê-lo, excede manifestamente os limites impostos pelo seu fim econômico ou social, pela boa-fé ou pelos bons costumes");
- **Violação da boa-fé objetiva** nas relações contratuais (art. 422 do Código Civil);
- Eventual enriquecimento sem causa (arts. 884 a 886 do Código Civil).

A plataforma e o produtor de conteúdo possuem legitimidade ativa para contestar o estorno e adotar medidas judiciais cabíveis. **O Katomart não oferece nem oferecerá qualquer mecanismo destinado a viabilizar, ocultar ou facilitar essa prática.**

#### 4.3.2. Rateio e Compartilhamento de Contas

A prática conhecida como "rateio" — múltiplas pessoas financiando coletivamente o acesso a uma mesma conta com vistas a consumo individualizado — não constitui exercício legítimo do direito à cópia privada.

A licença de acesso oferecida pela maioria das plataformas é **individual e intransferível**. A redistribuição de materiais baixados via Katomart no contexto de grupos de rateio configura:

- **Violação de direitos autorais por redistribuição não autorizada**, nos termos da Lei nº 9.610/98 e do art. 184 do Código Penal (em sua redação principal, sem o amparo do § 4º);
- Eventual descumprimento contratual com a plataforma, com responsabilização civil do titular da conta.

O titular da conta que fornece credenciais de acesso a terceiros, ou que distribui materiais obtidos através de tais credenciais, **assume integralmente a responsabilidade civil e criminal** pelas cópias derivadas geradas. Tal titular figura como destinatário direto de eventuais notificações judiciais e administrativas dos detentores de direitos.

#### 4.3.3. Redistribuição Não Autorizada

A prática de adquirir conteúdo em plataforma, realizar download via Katomart e, em seguida, **disponibilizar tal conteúdo a terceiros sem autorização** do titular dos direitos — independentemente de haver ou não contraprestação financeira — extrapola integralmente o amparo legal da cópia privada previsto no art. 184, § 4º do Código Penal.

A exceção legal é restrita a cópia em **um só exemplar, para uso privado do copista**. A redistribuição a terceiros, ainda que gratuita e sem fim lucrativo aparente, descaracteriza a cópia como "privada" e sujeita o redistribuidor à redação principal do art. 184 do Código Penal, sem o amparo do § 4º.

Configuram tal conduta, exemplificativamente:

- Disponibilização do material em canais públicos ou semipúblicos (Telegram, Discord, fóruns, redes sociais);
- Hospedagem do material em serviços de armazenamento em nuvem com link de acesso compartilhado (Mega, Google Drive, MediaFire, etc.);
- Distribuição via redes peer-to-peer (torrent, eMule e similares);
- Envio direto a terceiros que não detenham acesso legítimo ao conteúdo na plataforma de origem.

A ausência de cobrança financeira **não descaracteriza** o ilícito. O bem jurídico tutelado pela legislação autoral é a exclusividade de exploração econômica e moral da obra pelo titular, e não exclusivamente o lucro do redistribuidor. A jurisprudência brasileira é consolidada quanto à ilicitude de redistribuição não autorizada, mesmo em contextos não comerciais.

O usuário que redistribui materiais obtidos via Katomart **assume integralmente a responsabilidade civil e criminal** pela conduta, podendo responder por:

- Violação de direitos autorais nos termos do art. 184, *caput*, do Código Penal;
- Reparação civil por danos materiais e morais ao titular, nos termos da Lei nº 9.610/98 (arts. 102 e seguintes);
- Eventual responsabilização adicional por descumprimento contratual com a plataforma.

### 4.4. Ausência de Garantias

O Katomart é distribuído **"como está"** (as is), nos termos usuais do software open-source. O mantenedor não oferece garantia de:

- Disponibilidade contínua de compatibilidade com qualquer plataforma específica;
- Funcionamento ininterrupto ou ausência de falhas;
- Adequação a finalidade específica do usuário;
- Resultado particular esperado pelo usuário.

Eventuais danos decorrentes do uso do software — incluindo perda de dados, falhas de execução, ou consequências jurídicas resultantes de uso fora das hipóteses legais previstas — são de **responsabilidade exclusiva do usuário final**.

### 4.5. Aceitação Tácita

A obtenção, instalação, compilação ou execução do Katomart implica conhecimento e aceitação integral dos termos desta seção e demais cláusulas deste documento. O usuário que não concorde com qualquer dos termos aqui estabelecidos deve abster-se de utilizar o software.

---

## Observações Finais

Este documento é parte integrante do projeto Katomart e está sujeito a revisões periódicas. Versões anteriores permanecem acessíveis pelo histórico de commits do repositório.

Em caso de divergência entre este documento e comunicações informais (incluindo mensagens em canais de comunidade, redes sociais ou interações via bots), prevalece o presente documento publicado no repositório oficial.

Para dúvidas, esclarecimentos ou tratativas formais, o canal oficial é:

**victorhugoc.dosreissantos@gmail.com**

