from typing import Any, Dict, List, Optional
from pathlib import Path
import requests
import logging
import json
from src.platforms.base import BasePlatform, PlatformFactory
from src.app.models import LessonContent, Description, AuxiliaryURL, Video, Attachment
from src.config.settings_manager import SettingsManager
from src.app.api_service import ApiService

class HotmartPlatform(BasePlatform):
    """Implements the specific scraping logic for Hotmart."""
    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)

    def authenticate(self, credentials: Dict[str, str]) -> None:
        """Creates an authenticated session for the Hotmart API."""
        token = credentials.get("token")
        if not token:
            raise ValueError("Authentication token not provided.")
        
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Origin": "https://consumer.hotmart.com",
            "Referer": "https://consumer.hotmart.com/",
        })
    
    def get_session(self) -> Optional[requests.Session]:
        return self._session
        
    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetches paid and free courses from the Hotmart API."""
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        paid_response = self._session.get("https://api-hub.cb.hotmart.com/club-drive-api/rest/v2/purchase/?archived=UNARCHIVED")
        free_response = self._session.get("https://api-hub.cb.hotmart.com/club-drive-api/rest/v1/purchase/free/?archived=UNARCHIVED")
        paid_response.raise_for_status()
        free_response.raise_for_status()

        paid_list = self._extract_course_data(paid_response.json(), "[PAGO] ")
        free_list = self._extract_course_data(free_response.json(), "[GRATIS] ")

        combined = {course["id"]: course for course in paid_list + free_list}
        return sorted(list(combined.values()), key=lambda c: c["id"])

    def _extract_course_data(self, response_json: Dict[str, Any], prefix: str) -> List[Dict[str, Any]]:
        """Extracts and formats course data from the API response."""
        courses = []
        for item in response_json.get("data", []):
            product = item.get("product", {})
            if not product:
                continue
            
            course_data = {
                "id": product.get("id"),
                "name": f'{prefix}{product.get("name", "Unnamed")}',
                "seller_name": product.get("seller", {}).get("name"),
                "slug": product.get("hotmartClub", {}).get("slug")
            }
            if course_data["id"]:
                courses.append(course_data)
        return courses

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        """Fetches video URLs and other details for a specific lesson."""
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        lesson_hash = lesson.get("id")
        if not lesson_hash:
            raise ValueError("Lesson ID (hash) is missing.")

        headers = self._session.headers.copy()
        headers["slug"] = course_slug
        headers["x-app-name"] = "app-club-consumer_v1.93.3_production"
        headers["x-product-id"] = course_id
        
        url = f"https://api-club-course-consumption-gateway.hotmart.com/v2/web/lessons/{lesson_hash}"
        response = self._session.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        # with open("debug_hotmart_lesson.json", "w", encoding="utf-8") as f:
        #     json.dump(data, f, indent=2, ensure_ascii=False)
        # input("Press Enter to continue...")

        content = LessonContent()

        if description := data.get("content"):
            content.description = Description(text=description, description_type="html")

        for video_index, video_data in enumerate(data.get("medias", []), start=1):
            content.videos.append(Video(
                video_id=video_data.get("code"),
                url=video_data.get("url"),
                order=video_data.get("order", video_index),
                title=video_data.get("name", "video"),
                size=video_data.get("size", 0),
                duration=video_data.get("duration", 0)
            ))

        attachment_url = f"https://api-club-course-consumption-gateway-ga.cb.hotmart.com/v1/pages/{lesson_hash}/complementary-content"
        attachment_response = self._session.get(attachment_url, headers=headers)
        attachment_response.raise_for_status()
        attachment_json = attachment_response.json()

        for attachment_index, att in enumerate(attachment_json.get("attachments", []), start=1):
            file_membership_id = att.get("fileMembershipId", "")
            filename = att.get("fileName", "")
            extension = filename.split(".")[-1] if "." in filename else ""
            order = att.get("fileOrder", attachment_index)
            url = att.get("fileUrl", "")
            size = att.get("fileSize", 0)
            content.attachments.append(
                Attachment(
                    attachment_id=file_membership_id,
                    url=url,
                    filename=filename,
                    order=order,
                    extension=extension,
                    size=size,
                )
            )

        for reading_index, reading in enumerate(data.get("complementaryReadings", []), start=1):
            url_id = reading.get("id", reading.get("articleUrl", ""))
            url = reading.get("articleUrl", "")
            order = reading.get("order", reading_index)
            title = reading.get("articleName", "")
            site_name = reading.get("siteName", "")
            description = f"{title} ({site_name})" if site_name else title
            content.auxiliary_urls.append(
                AuxiliaryURL(
                    url_id=url_id,
                    url=url,
                    order=order,
                    title=title,
                    description=description,
                )
            )

        return content

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        """
        Downloads an attachment from Hotmart, handling different response schemes.
        """
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")
        
        instruction_url = f"https://api-club-hot-club-api.cb.hotmart.com/rest/v3/attachment/{attachment.attachment_id}/download"

        logging.info(f"Fetching download instructions for attachment: {attachment.filename}")
        
        try:
            headers = self._session.headers.copy()
            headers.update({
                "x-product-id": course_id,
                "x-app-name": "app-club-consumer_v1.227.1_production",
                "Referer": "https://hotmart.com/",
                "Origin": "https://hotmart.com/",
            })

            instruction_response = self._session.get(instruction_url, headers=headers)
            instruction_response.raise_for_status()
            instructions = instruction_response.json()

            if 'directDownloadUrl' in instructions:
                direct_url = instructions['directDownloadUrl']
                logging.info(f"Anexo sem marca dagua, baixando de: {direct_url}")

                file_response = self._session.get(direct_url, stream=True)
                file_response.raise_for_status()

                with open(download_path, 'wb') as f:
                    for chunk in file_response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                logging.info(f"Anexo salvo com sucesso {download_path}")
                return True

            elif 'lambdaUrl' in instructions and 'token' in instructions:
                lambda_url = instructions['lambdaUrl']
                token = instructions['token']
                logging.info(f"Anexo com marca dagua, baixando de: {lambda_url}")

                lambda_session = requests.Session()
                lambda_session.headers.update({
                    "User-Agent": self._settings.user_agent,
                    "token": token
                })

                lambda_response = lambda_session.get(lambda_url)
                lambda_response.raise_for_status()

                final_attachment_url = lambda_response.content.decode('utf-8')
                logging.debug(f"URL lambda: {final_attachment_url}")

                final_response = requests.get(final_attachment_url, stream=True)
                final_response.raise_for_status()
                
                with open(download_path, 'wb') as f:
                    for chunk in final_response.iter_content(chunk_size=8192):
                        f.write(chunk)

                logging.info(f"Anexo salvo com sucesso {download_path}")
                return True

            else:
                logging.error(f"Unknown attachment response scheme for '{attachment.filename}'.")
                logging.error(f"Instructions received: {instructions}")
                return False

        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to download attachment '{attachment.filename}': {e}")
            return False
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Failed to process or write attachment file for '{attachment.filename}': {e}")
            return False

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetches the content (modules/lessons) for the selected courses."""
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")
        
        all_content = {}
        for course in courses:
            headers = self._session.headers.copy()
            headers.update({
                "slug": course["slug"],
                "x-product-id": str(course["id"]),
            })
            response = self._session.get(
                "https://api-club-course-consumption-gateway.hotmart.com/v1/navigation",
                headers=headers
            )
            response.raise_for_status()
            
            api_response = response.json()

            logging.debug("--- Hotmart API Response for Course Content ---")
            logging.debug(json.dumps(api_response, indent=2))
            logging.debug("---------------------------------------------")

            processed_modules = []
            for module_data in api_response.get("modules", []):
                module_data["title"] = module_data.get("name", "Untitled Module")
                
                lessons = []
                for lesson_data in module_data.get("pages", []):
                    lesson_data["title"] = lesson_data.get("name", "Untitled Lesson")
                    lesson_data["id"] = lesson_data.get("hash")
                    lessons.append(lesson_data)
                
                module_data["lessons"] = lessons
                if "pages" in module_data:
                    del module_data["pages"]

                processed_modules.append(module_data)

            course_with_modules = course.copy()
            course_with_modules["modules"] = processed_modules
            course_with_modules["title"] = course.get("name", "Untitled Course")

            all_content[course["id"]] = course_with_modules
        return all_content

PlatformFactory.register_platform("Hotmart", HotmartPlatform)
