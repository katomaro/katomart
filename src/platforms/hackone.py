from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src.platforms.base import AuthField, PlatformFactory
from src.platforms.kajabi import KajabiPlatform

logger = logging.getLogger(__name__)

HACKONE_BASE_URL = "https://app.hackone.com.br"


class HackOnePlatform(KajabiPlatform):
    """
    HackOne (app.hackone.com.br) — Kajabi-hosted site with a custom theme and a
    three-level content hierarchy on the /library page:

        trilhas de estudo  (Kajabi products)
            └── treinamentos  (top-level Kajabi categories)
                    ├── aulas directly inside the treinamento (posts), and/or
                    └── módulos  (sub-categories) — each holds its own aulas

    A treinamento may have any combination: only direct aulas, only módulos, or
    both. The 3-level tree is flattened into the platform's 2-level Course →
    Module → Lesson model: each (treinamento, optional módulo) pair becomes one
    module entry. When a treinamento has direct aulas, those become a module
    titled after the treinamento itself; each child módulo becomes an additional
    module titled "<treinamento> :: <módulo>".

    Selectors are URL-pattern based rather than CSS-class based — HackOne's
    theme replaces the default Kajabi `.syllabus__item` markup with custom
    `.card.media.post-listing` cards, but every nav anchor still follows the
    canonical Kajabi URL shape (`/products/<slug>/categories/<id>` for
    categories and sub-categories, `…/posts/<id>` for posts), so href regex
    extraction is more resilient to theme tweaks.
    """

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        # site_url is fixed for HackOne — no extra prompt needed.
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter o token do HackOne:
1) Abra https://app.hackone.com.br no seu navegador e faça login normalmente.
2) Pressione F12 e vá na aba Application > Cookies > app.hackone.com.br.
3) Copie o valor do cookie "_kjb_session".
4) Cole no campo Token acima.

Assinantes ativos podem informar usuário e senha o app faz login pelo
formulário do Kajabi e captura o cookie automaticamente.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        # Inject the fixed HackOne URL transparently so we can reuse the Kajabi
        # auth flow (CSRF + login form submit + cookie capture) without asking
        # the user to type the host.
        merged = {**credentials, "site_url": HACKONE_BASE_URL}
        super().authenticate(merged)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        merged = {**credentials, "site_url": HACKONE_BASE_URL}
        return super()._exchange_credentials_for_token(username, password, merged)

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content: Dict[str, Any] = {}
        for course in courses:
            slug = course.get("slug") or course.get("id")
            if not slug:
                continue

            modules = self._build_modules_for_trilha(str(slug))
            entry = course.copy()
            entry["title"] = course.get("name", slug)
            entry["modules"] = modules
            all_content[str(slug)] = entry

        return all_content

    def _build_modules_for_trilha(self, trilha_slug: str) -> List[Dict[str, Any]]:
        treinamentos = self._list_treinamentos(trilha_slug)
        modules: List[Dict[str, Any]] = []
        order = 0

        for tr in treinamentos:
            tr_id = tr["id"]
            tr_title = tr["title"]

            direct_aulas, sub_modulos = self._parse_category_page(
                self._fetch_category_html(trilha_slug, tr_id),
                trilha_slug,
                tr_id,
            )

            if direct_aulas:
                order += 1
                modules.append({
                    "id": str(tr_id),
                    "title": tr_title,
                    "order": order,
                    "lessons": [
                        {
                            "id": p["id"],
                            "title": p["title"],
                            "order": i + 1,
                            "category_id": p.get("category_id") or tr_id,
                            "locked": False,
                        }
                        for i, p in enumerate(direct_aulas)
                    ],
                    "locked": False,
                })

            for modulo in sub_modulos:
                modulo_id = modulo["id"]
                modulo_title = modulo["title"]
                aulas_in_modulo, _ = self._parse_category_page(
                    self._fetch_category_html(trilha_slug, modulo_id),
                    trilha_slug,
                    modulo_id,
                )
                if not aulas_in_modulo:
                    continue
                order += 1
                modules.append({
                    "id": str(modulo_id),
                    "title": f"{tr_title} :: {modulo_title}",
                    "order": order,
                    "lessons": [
                        {
                            "id": p["id"],
                            "title": p["title"],
                            "order": i + 1,
                            "category_id": p.get("category_id") or modulo_id,
                            "locked": False,
                        }
                        for i, p in enumerate(aulas_in_modulo)
                    ],
                    "locked": False,
                })

        logger.info("HackOne: trilha %s rendered as %d module(s)", trilha_slug, len(modules))
        return modules

    def _list_treinamentos(self, trilha_slug: str) -> List[Dict[str, Any]]:
        """
        Hits /products/<slug>/categories — the trilha's "Treinamentos" index —
        and pulls out every top-level treinamento card by URL shape.
        """
        url = f"{self._base_url}/products/{trilha_slug}/categories"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("HackOne: falha ao listar treinamentos de %s: %s", trilha_slug, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        category_re = re.compile(
            rf"^/products/{re.escape(trilha_slug)}/categories/(\d+)/?$"
        )

        treinamentos: List[Dict[str, Any]] = []
        seen: set = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http"):
                href = urlparse(href).path
            m = category_re.match(href)
            if not m:
                continue
            cat_id = m.group(1)
            if cat_id in seen:
                continue
            seen.add(cat_id)

            title = self._extract_card_title(a) or f"Treinamento {cat_id}"
            treinamentos.append({"id": cat_id, "title": title})

        logger.info(
            "HackOne: trilha %s tem %d treinamentos", trilha_slug, len(treinamentos)
        )
        return treinamentos

    def _fetch_category_html(self, trilha_slug: str, category_id: str) -> str:
        url = f"{self._base_url}/products/{trilha_slug}/categories/{category_id}"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.error(
                "HackOne: falha ao buscar categoria %s/%s: %s",
                trilha_slug, category_id, exc,
            )
            return ""

    def _parse_category_page(
        self, html: str, trilha_slug: str, current_category_id: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Walks anchors on a category page and splits them into:
        - aulas: anchors to /products/<slug>/categories/<id>/posts/<post_id>
          (post may be in current category or in a child sub-category)
        - sub_modulos: anchors to /products/<slug>/categories/<other_id> with
          other_id != current_category_id

        Returns (aulas_in_order, sub_modulos_in_order).
        """
        if not html:
            return [], []

        soup = BeautifulSoup(html, "html.parser")
        post_re = re.compile(
            rf"^/products/{re.escape(trilha_slug)}/categories/(\d+)/posts/(\d+)/?$"
        )
        category_re = re.compile(
            rf"^/products/{re.escape(trilha_slug)}/categories/(\d+)/?$"
        )

        aulas: List[Dict[str, Any]] = []
        seen_posts: set = set()
        sub_modulos: List[Dict[str, Any]] = []
        seen_subs: set = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http"):
                href = urlparse(href).path

            m_post = post_re.match(href)
            if m_post:
                cat_id, post_id = m_post.group(1), m_post.group(2)
                if post_id in seen_posts:
                    continue

                title = self._extract_card_title(a)
                # Skip the "Começar agora" / continuation buttons that link to
                # a deep post but carry no real lesson title.
                if not title or _is_skip_button(a, title):
                    continue

                seen_posts.add(post_id)
                aulas.append({"id": post_id, "title": title, "category_id": cat_id})
                continue

            m_cat = category_re.match(href)
            if m_cat:
                cat_id = m_cat.group(1)
                if cat_id == current_category_id or cat_id in seen_subs:
                    continue

                title = self._extract_card_title(a)
                if not title:
                    continue

                seen_subs.add(cat_id)
                sub_modulos.append({"id": cat_id, "title": title})

        return aulas, sub_modulos

    @staticmethod
    def _extract_card_title(anchor) -> str:
        """
        Pulls a meaningful title out of a HackOne nav anchor. The custom theme
        wraps the title in a `.title` / `.post-title` / `.media-body p` element
        when the anchor itself is image-only; falls back to anchor text.
        """
        for sel in (".title", ".post-title", ".card-block .title", ".media-body .title"):
            el = anchor.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                if txt:
                    return txt
        text = anchor.get_text(" ", strip=True)
        return text


def _is_skip_button(anchor, title: str) -> bool:
    cls = " ".join(anchor.get("class") or []).lower()
    if "btn--skip" in cls:
        return True
    lowered = title.strip().lower()
    return lowered in {
        "começar agora", "comecar agora", "começar", "comecar",
        "continuar", "continue", "start now",
    }


PlatformFactory.register_platform("HackOne", HackOnePlatform)
