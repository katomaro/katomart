# Legal Policies — Katomart

**Last updated:** May 7, 2026
**Maintainer:** Victor Hugo Carvalho dos Reis Santos
**Official contact:** victorhugoc.dosreissantos@gmail.com

---

## ⚠️ Language Precedence Notice

This document is a **courtesy translation** of the original Portuguese-language `LEGAL.md` available in this repository. The Katomart project originates in **Brazil**, and its legal framework is grounded in **Brazilian federal law**.

**In any case of divergence, ambiguity, or interpretive doubt between this English version and the original Portuguese version, the Portuguese version prevails as the authoritative text**, both because Portuguese is the native language of the maintainer and because the legal references herein are inherently tied to Brazilian statutory law.

The original document is available at [`LEGAL.md`](./LEGAL.md) (or equivalent path in the repository root).

---

## Table of Contents

1. [Nature of the Project](#1-nature-of-the-project)
2. [Extrajudicial Notification Policy (DMCA / Takedown)](#2-extrajudicial-notification-policy-dmca--takedown)
3. [Technical Engagement with Platforms](#3-technical-engagement-with-platforms)
4. [Legal Basis of Use and End-User Responsibility](#4-legal-basis-of-use-and-end-user-responsibility)

---

## 1. Nature of the Project

**Katomart** is open-source software, distributed under a public license, that operates strictly as a **local automation client**. Its core architectural characteristics are:

- **Exclusively local execution:** all processing takes place on the end user's machine.
- **Authentication via the user's own credentials:** the software does not hold, store, or distribute credentials. Access to any supported platform depends entirely on legitimate credentials owned by the user.
- **No hosting or redistribution:** Katomart does not host, transmit, intermediate traffic, or redistribute content from third-party platforms. Any content processed remains on the user's local machine.
- **Multi-platform support:** the project supports dozens of distinct platforms, configuring it as a general-purpose tool, not directed at any specific platform or use case.

The technical operation of Katomart is functionally equivalent to that of an authenticated web browser, differing only in the automation of operational steps.

---

## 2. Extrajudicial Notification Policy (DMCA / Takedown)

### 2.1. Legal Basis

Pursuant to **Article 19 of Law No. 12,965/2014 (Brazilian Internet Civil Framework — *Marco Civil da Internet*)**, the removal of content, code, or functionality from the official repository will be carried out only upon **specific judicial order**, except in the exceptional cases expressly provided by law or under the expanded liability scenarios established by the Brazilian Federal Supreme Court (STF) in the judgment of Theme 987 (RE 1037396).

Extrajudicial notifications, on their own, do not create an obligation to remove content, but will be examined seriously when properly substantiated and submitted through the correct channel.

### 2.2. Formal Requirements for Notifications

For a notification to be processed, the request must cumulatively meet the following requirements:

**a) Verifiable origin:**
- Communication from a **verifiable corporate domain** of the platform or rights-holding entity; **or**
- Presentation of a **valid digital certificate** or **PGP signature** linked to the represented entity; **or**
- Presentation of a **specific formal power of attorney** with express powers for extrajudicial representation in this case, when the notification originates from intermediaries.

**b) Specific legal grounds:**
- Precise indication of the legal provision allegedly violated;
- Technical and specific description of the alleged violation, with exact identification of the code excerpt, file, or functionality at issue;
- Reproducible technical evidence, where applicable.

**c) Full identification of the requester:**
- Full name of the legal representative;
- Position and affiliation with the represented entity;
- Direct contact information (not exclusively through an intermediary).

### 2.3. Notifications Originated by Intermediaries

Notifications originated by **brand protection companies, content monitoring firms, digital rights management agencies, or third-party intermediaries** (including, but not limited to, companies such as Axur, MarkMonitor, Incopro, Red Points, and similar) will only be processed upon:

1. Presentation of a specific formal power of attorney for the case, in the name of the entity holding the alleged rights, with express powers for extrajudicial representation; **and**
2. Possibility of direct validation with the represented entity through a verifiable corporate channel.

Notifications that fail to meet these requirements will be archived without further processing.

### 2.4. Claims Not Covered by This Policy

The following allegations **do not constitute valid grounds** for the removal of code or functionality from the official repository:

- **Citation of registered trademarks** in an informational, technical, or systemic-compatibility-identification capacity, as supported by Article 132, IV of Law No. 9,279/96 (Brazilian Industrial Property Law).
- **Technical documentation of endpoints** publicly observable by authenticated users via standard developer tools, which does not constitute a trade secret or personal data protected by Law No. 13,709/2018 (LGPD — Brazilian General Data Protection Law).
- **Abstract possibility of misuse** by third parties, in the absence of elements characterizing the tool as directed at unlawful purposes or as lacking substantial legitimate utility.
- **Generic allegation of "facilitation"** unaccompanied by technical and legal demonstration of a concrete violation attributable to the project maintainer.

### 2.5. Transparency and Publication

Received notifications may be published in their entirety in this repository, with redaction of sensitive personal data where applicable, in line with practices established by equivalent open-source projects (such as the treatment GitHub gives to DMCA counter-notices).

The purpose of such publication is community transparency and the construction of public precedent regarding the handling of such requests.

### 2.6. Official Channel

Notifications must be sent exclusively to:

**victorhugoc.dosreissantos@gmail.com**

(the same public address used in commits and on the maintainer's official profiles)

---

## 3. Technical Engagement with Platforms

Independently of the legal stance set forth above, the project **remains open to legitimate technical engagement** with platforms that demonstrate interest in constructive collaboration. Substantiated technical requests will be actively evaluated, including:

### 3.1. Request Adjustments

Inclusion, removal, or adaptation of calls to specific endpoints (for example: analytics endpoints, internal telemetry, or administrative routes that should not be reached by client-side automation).

**Requirements:** technical presentation of the impact, payload example, and justification.

### 3.2. Traffic Control (Rate Limiting)

Implementation of maximum-speed (throttling) limits on the scraping behavior toward the requesting platform, upon demonstration that the volume of requests originating from Katomart instances is causing verified anomalies in the infrastructure.

**Requirements:**
- Reproducible technical evidence;
- Clear identification of Katomart's discriminator pattern in the traffic (User-Agent, headers, request pattern);
- Demonstration that the anomaly is attributable to Katomart and not to aggregate behavior of the platform's legitimate users.

### 3.3. Data Integrity

Katomart preserves the content delivered by the platform in the form in which it is delivered. **Hidden metadata, digital watermarks, or tracking mechanisms embedded into the delivery stream by the platform remain intact** in the local file generated by the user, without any interference or removal attempt by the software.

### 3.4. Transparency of Changes

**Every accepted change will be public in this repository.** Katomart does not implement obfuscation mechanisms. Users with technical knowledge retain full freedom to create forks or compile their own versions without agreed-upon restrictions — the maintenance of such restrictions in the official repository will be handled with full community transparency.

### 3.5. Pull Requests

Technical Pull Requests documenting compatibility adjustments will be evaluated under the project's usual technical-quality criteria, regardless of the corporate or individual origin of the contributor.

### 3.6. Channel for Technical Engagement

Same address as section 2.6:

**victorhugoc.dosreissantos@gmail.com**

Technical engagement should preferably originate from members of the platform's engineering team, with verifiable identification.

---

## 4. Legal Basis of Use and End-User Responsibility

The operation of Katomart is supported by Brazilian law, the country of origin of the project. The software's operation finds specific legal grounding in **Article 184, § 4º of the Brazilian Penal Code**, which establishes the exceptions to copyright-related criminal offenses:

> **Article 184. Violating copyright and related rights:**
> Penalty — detention, from 3 (three) months to 1 (one) year, or fine.
> [...]
> **§ 4º** The provisions of §§ 1º, 2º, and 3º shall not apply when the matter concerns an exception or limitation to copyright or related rights, in accordance with Law No. 9,610 of February 19, 1998, nor to the copy of an intellectual work or phonogram, in a single specimen, for the private use of the copier, without direct or indirect profit motive.

*(Free translation of the original Portuguese text. The original prevails.)*

### 4.1. Right to Private Copy

Brazilian federal law expressly recognizes the right of a holder of legitimate access to content to produce a **single copy for private use**, provided there is no direct or indirect profit motive.

In practical terms: a user who holds **authenticated and legitimate access** to material made available on a third-party platform is supported by federal law in producing a local copy intended for strictly private use.

**The Terms of Service (ToS) of platforms or content producers do not have sufficient legal force to override a statutory guarantee provided by federal law.** The contractual relationship between platform and user cannot, in a restrictive sense, override rights guaranteed by statute.

### 4.2. Exclusive Responsibility of the End User

Katomart is a **neutral automation tool**. The software's operation depends entirely on execution by the end user, using their own credentials. The following fall **exclusively** upon the user, with no subsidiary or joint liability of the project maintainer:

- The legitimacy of the credentials used;
- Compliance of the use with the contractual terms accepted by the user on each platform;
- The framing of the produced copy within the legal grounds for private use;
- The final destination of any downloaded content.

The project maintainer has no visibility, control, or technical capacity to audit instances of the software run by third parties.

### 4.3. Repudiated Conduct

Although Katomart is a neutral tool and its maintainer does not exert control over individual use, this project **expressly repudiates** the following conduct, which exceeds the legal protection afforded to private copying and constitutes autonomous unlawful acts:

#### 4.3.1. Abuse of the Right of Withdrawal

The practice of acquiring content on a platform, performing a full download via Katomart, and subsequently requesting a refund based on **Article 49 of the Brazilian Consumer Defense Code (CDC)** (right of withdrawal), with the sole purpose of obtaining the content without effective financial consideration, constitutes:

- **Abuse of right**, under Article 187 of the Brazilian Civil Code ("the holder of a right also commits an unlawful act when, in exercising it, manifestly exceeds the limits imposed by its economic or social purpose, by good faith, or by good morals");
- **Violation of objective good faith** in contractual relations (Article 422 of the Brazilian Civil Code);
- Possible unjust enrichment (Articles 884 to 886 of the Brazilian Civil Code).

The platform and the content producer have active legitimacy to contest the refund and to take applicable judicial measures. **Katomart does not and will not offer any mechanism intended to enable, conceal, or facilitate this practice.**

#### 4.3.2. Account Sharing and Cost-Splitting ("Rateio")

The practice known in Brazil as *"rateio"* — multiple persons collectively financing access to a single account for individualized consumption — does not constitute legitimate exercise of the right to private copy.

The access license offered by most platforms is **individual and non-transferable**. The redistribution of materials downloaded via Katomart in the context of cost-splitting groups constitutes:

- **Copyright violation through unauthorized redistribution**, under Law No. 9,610/98 and Article 184 of the Brazilian Penal Code (in its main wording, without the protection of § 4º);
- Possible contractual breach with the platform, with civil liability falling on the account holder.

The account holder who provides access credentials to third parties, or who distributes materials obtained through such credentials, **fully assumes civil and criminal liability** for the derivative copies generated. Such holder figures as the direct addressee of any judicial and administrative notifications from rights holders.

#### 4.3.3. Unauthorized Redistribution

The practice of acquiring content on a platform, performing a download via Katomart, and subsequently **making such content available to third parties without authorization** from the rights holder — regardless of whether there is financial consideration — fully exceeds the legal protection afforded to private copying under Article 184, § 4º of the Brazilian Penal Code.

The legal exception is restricted to a copy in a **single specimen, for the private use of the copier**. Redistribution to third parties, even when free and without apparent profit motive, removes the copy from the "private" category and subjects the redistributor to the main wording of Article 184 of the Brazilian Penal Code, without the protection of § 4º.

Such conduct includes, by way of example:

- Making the material available on public or semi-public channels (Telegram, Discord, forums, social networks);
- Hosting the material on cloud storage services with shared access links (Mega, Google Drive, MediaFire, etc.);
- Distribution via peer-to-peer networks (torrent, eMule, and similar);
- Direct sending to third parties who do not hold legitimate access to the content on the originating platform.

The absence of financial charging **does not remove the unlawful character** of the act. The legal interest protected by copyright legislation is the rights holder's exclusive economic and moral exploitation of the work, not exclusively the redistributor's profit. Brazilian case law is well-established regarding the unlawfulness of unauthorized redistribution, even in non-commercial contexts.

A user who redistributes materials obtained via Katomart **fully assumes civil and criminal liability** for such conduct, and may be held accountable for:

- Copyright violation under Article 184, *caput*, of the Brazilian Penal Code;
- Civil compensation for material and moral damages to the rights holder, under Law No. 9,610/98 (Articles 102 et seq.);
- Possible additional liability for contractual breach with the platform.

### 4.4. Disclaimer of Warranties

Katomart is distributed **"as is"**, under the usual terms of open-source software. The maintainer offers no warranty of:

- Continuous availability of compatibility with any specific platform;
- Uninterrupted operation or absence of failures;
- Suitability for the user's specific purpose;
- Particular result expected by the user.

Any damages arising from the use of the software — including data loss, execution failures, or legal consequences resulting from use outside the established legal grounds — are the **exclusive responsibility of the end user**.

### 4.5. Tacit Acceptance

Obtaining, installing, compiling, or executing Katomart implies knowledge and full acceptance of the terms of this section and of the remaining clauses of this document. A user who does not agree with any of the terms set forth herein must refrain from using the software.

---

## Final Remarks

This document is an integral part of the Katomart project and is subject to periodic revision. Previous versions remain accessible through the repository's commit history.

In case of divergence between this document and informal communications (including messages on community channels, social networks, or interactions via bots), this document published in the official repository prevails.

**As stated at the beginning of this document, in case of divergence between this English version and the original Portuguese version, the Portuguese version prevails.**

For inquiries, clarifications, or formal engagement, the official channel is:

**victorhugoc.dosreissantos@gmail.com**

