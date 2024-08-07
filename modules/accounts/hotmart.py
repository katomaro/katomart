"""Código referente ao consumo de produtos da Hotmart"""

from .abstract import Account
import time

class Hotmart(Account):
    """
    Representa um usuário da Hotmart, especializando a classe Account para
    lidar com as especificidades desta plataforma.
    """
    
    def __init__(self, account_id: int = 0, database_manager=None):
        """
        Inicializa uma instância de Hotmart.

        :param username: Nome de usuário ou e-mail.
        :param password: Senha da conta.
        :param database_manager: Gerenciador de banco de dados para esta conta.
        """
        super().__init__(account_id=account_id, database_manager=database_manager)
        self.platform_id = self.get_platform_id()
        # Estas URLs estão para mudar!
        self.LOGIN_URL = 'https://sec-proxy-content-distribution.hotmart.com/club/security/oauth/token'
        self.PRODUCTS_URL = 'https://sec-proxy-content-distribution.hotmart.com/club/security/oauth/check_token'
        self.MEMBER_AREA_URL = 'https://api-club.cb.hotmart.com/rest/v3/navigation'
        self.CLUB_API = 'https://api-club-hot-club-api.cb.hotmart.com/rest/v3'

        self.load_account_information()
        self.load_tokens()
        self.login()

    def get_platform_id(self):
        """
        Retorna o ID da plataforma de cursos.
        """
        platform_id = self.database_manager.execute_query(
            'SELECT id FROM platforms WHERE name = ? LIMIT 1', 
            ('Hotmart',), 
            fetchone=True
            )
        return platform_id[0]

    def login(self):
        """
        Realiza o login na conta da Hotmart, autenticando o usuário e obtendo tokens de acesso.
        """
        if not self.auth_token or self.auth_token_expires_at < self.get_current_time():
            login_data = {
                'grant_type': 'password',
                'username': self.username,
                'password': self.password
            }
            response = self.session.post(self.LOGIN_URL, data=login_data)
            print('[DEBUG] Chamada POST para login na Hotmart.', response.status_code, response.url, response.text.encode('utf-8'))

            if response.status_code != 200:
                raise Exception(f'Erro ao acessar {response.url}: Status Code {response.status_code}')

            response = response.json()
            self.auth_token = response['access_token']
            self.auth_token_expires_at = self.get_current_time() + response['expires_in']
            self.refresh_token = response['refresh_token']
            self.refresh_token_expires_at = self.get_current_time() + response['expires_in']
            self.other_data = self.dump_json_data(response)
            self.database_manager.execute_query("""
                INSERT OR REPLACE INTO Auths (account_id, platform_id, auth_token, auth_token_expires_at, refresh_token, refresh_token_expires_at, other_data)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.account_id, self.platform_id, self.auth_token, self.auth_token_expires_at, self.refresh_token, self.refresh_token_expires_at, self.other_data)
            )

    def refresh_auth_token(self):
        """
        Renova o token de acesso da conta.
        """
        pass

    def get_account_products(self, get_extra_info: int = 0):
        """
        Retorna os produtos associados à conta do usuário na Hotmart.
        """
        data = {
            'token': self.auth_token
        }
        response = self.session.get(self.PRODUCTS_URL, params=data)
        print('[DEBUG] Chamada GET para obter produtos da Hotmart.', response.status_code, response.url, response.text)
        if response.status_code != 200:
            raise Exception(f'Erro ao acessar {response.url}: Status Code {response.status_code}')
        
        response = response.json()['resources']
        products = []
        for resource in response:
            course_information = None
            if resource.get('type') == 'PRODUCT':

                subdomain = resource.get('resource', {}).get('subdomain')
                composed_domain = f'https://{subdomain}.club.hotmart.com'


                if get_extra_info:
                    fake_session = self.clone_main_session()
                    headers = {}
                    headers['user-agent'] = fake_session.headers['user-agent']
                    headers['authorization'] = f'Bearer {self.auth_token}'

                    headers['origin'] = composed_domain
                    headers['referer'] = composed_domain
                    headers["accept"] = "application/json, text/plain, */*"
                    headers['club'] = subdomain
                    headers["pragma"] = "no-cache"
                    headers["cache-control"] = "no-cache"
                    fake_session.headers.update(headers)
                    course_information = fake_session.get(
                        f'{self.CLUB_API}/membership?attach_token=false'
                    ).json()
                    course_name = course_information.get('name', subdomain)
                    has_drm = int(course_information.get('protectionDrm', 0))
                    if has_drm:
                        has_drm = 2
                    elif course_information.get('streamingDrmProtection', False):
                        has_drm = 1
                    
                    # Segurança mínima para contas com muitos cursos
                    if len(response) > 10:
                        time.sleep(2)
                    
                    del fake_session

                if not course_information:
                    course_information = {}
                    has_drm = 0

                product_dict = {
                        'data': {
                            'drm_enabled': has_drm if get_extra_info else 0,
                            'name': course_name if get_extra_info else subdomain,
                            'id': int(resource.get('resource', {}).get('productId')),
                            'subdomain': subdomain,
                            'status': resource.get('resource', {}).get('status'),
                            'user_area_id': int(resource.get('resource', {}).get('userAreaId')),
                            'roles': resource.get('roles'),
                            'domain': composed_domain,
                            'modules': [],
                            'drm_data': {
                                'code': course_information.get('code', ''),
                                'product_id': course_information.get('productId', 0),
                                'membership_owner_hash': course_information.get('membershipOwnerHash', ''),
                            }
                        }
                }
                products.append(product_dict)
        
        return products

    def format_account_products(self, product_id: int | str | None = None, product_info: dict = None):
        """
        Desnecessario para a Hotmart.
        """
        print("I'm a little teapot, short and stout!")
    
    def get_content_module_info(self, product_info: dict, module_id: str | int):
        """
        Desnecessario para a Hotmart.
        """
        print("Here is my handle, here is my spout!")
    
    def get_content_lesson_info(self, content_id: str | int, domain: str, module_id: str | int, lesson_id: str | int):
        """
        Obtém os arquivos de uma lição da Hotmart.
        """
        print('[DEBUG] Obtendo informações da lição:', domain, lesson_id)
        lesson_url = f"{self.MEMBER_AREA_URL.rsplit('/', 1)[0]}/page/{lesson_id}?pageHash={lesson_id}"
        subdomain = domain.split('.')[0]
        composed_domain = domain
        fake_session = self.clone_main_session()
        headers = {}
        headers['user-agent'] = fake_session.headers['user-agent']
        headers['authorization'] = f'Bearer {self.auth_token}'
        headers['origin'] = composed_domain
        headers['referer'] = composed_domain
        headers["accept"] = "application/json, text/plain, */*"
        headers['club'] = subdomain.rsplit('/', 1)[-1]
        headers["pragma"] = "no-cache"
        headers["cache-control"] = "no-cache"
        fake_session.headers.update(headers)

        response = fake_session.get(lesson_url)
        print('[DEBUG] Chamada GET para obter informações da lição.', response.status_code, response.url, response.text)
        if response.status_code != 200:
            self.database_manager.log_event('critical', 0, f'Erro ao acessar {response.url}: Status Code {response.status_code}')
            return None
        response = response.json()
        full_lesson = {}
        if response.get('content'):
            full_lesson['text_content'] = response['content']
        if response.get('moduleCode'):
            full_lesson['modular_drm'] = response['moduleCode']
        if response.get('mediasSrc'):
            full_lesson['medias'] = response['mediasSrc']
        if response.get('attachments'):
            full_lesson['attachments'] = response['attachments']
        # if response.get('type') == 'WEBINAR':
        #     full_lesson['webinar'] = response['webinar']
        if response.get('complementaryReadings'):
            full_lesson['references'] = response['complementaryReadings']
        
        if full_lesson.get('medias'):
            print('[DEBUG] Medias:', full_lesson['medias'])
            for media in full_lesson['medias']:
                media['name'] = media.pop('mediaName')
                media['url'] = media.pop('mediaSrcUrl')
                media['type'] = media.pop('mediaType')
                media['hash'] = media.pop('mediaCode')
                media['size'] = media.pop('mediaSize')
                media['duration'] = media.pop('mediaDuration')
                media['is_stream'] = True
        
        if full_lesson.get('attachments'):
            for attachment in full_lesson['attachments']:
                attachment['is_stream'] = False
                attachment['name'] = attachment.pop('fileName')
                attachment['size'] = attachment.pop('fileSize')
                attachment['hash'] = attachment.pop('fileMembershipId')
        
        return full_lesson
    
    def format_product_information(self, product_info: dict):
        """
        Formata as informações de um produto específico associado à conta do usuário.
        """
        print('[DEBUG] Formatação de informações do produto:', product_info)
        product_info['modules'].sort(key=lambda x: x['moduleOrder'])
        for i, module in enumerate(product_info['modules'], start=1):
            module['moduleOrder'] = i
        
            sorted_pages = sorted(module['pages'], key=lambda x: x['pageOrder'])
            lessons = []
            for j, page in enumerate(sorted_pages, start=1):
                page['lessonOrder'] = j
                del page['pageOrder']
                page['id'] = page.pop('hash')
                if page.get('medias'):
                    page.pop('medias')
                #     page['files'] = page.pop('medias')
                lessons.append(page)
            
            module['lessons'] = lessons
            del module['pages']
        
        return product_info

    def get_product_information(self, product_id: str):
        """
        Retorna informações de um produto específico associado à conta do usuário.
        :club_name: nome da área de membros da htm.

        :return: Dicionário com informações do produto.
        """
        self.session.headers['authorization'] = f'Bearer {self.auth_token}'
        self.session.headers['club'] = product_id
        response = self.session.get(self.MEMBER_AREA_URL)
        print('[DEBUG] Chamada GET para obter informações do produto.', response.status_code, response.url, response.text)
        if response.status_code != 200:
            raise Exception(f'Erro ao acessar {response.url}: Status Code {response.status_code}')
        
        fmt_info = self.format_product_information(response.json())
        return fmt_info

    def download_content(self, product_info: dict = None):
        """
        Baixa o conteúdo de um produto específico associado à conta do usuário.
        """
        if product_info is None:
            print('[DEBUG] Nenhum produto foi passado para download.')
            return

        if not product_info['data']['modules']:
            subdomain = product_info['data'].get('subdomain', '')
            new_modules = self.get_product_information(subdomain)
            
            download_dict = {
                'save_path': product_info.get('save_path'),
                'data': product_info.get('data')
            }
            del download_dict['data']['modules']
            download_dict['data']['modules'] = new_modules.get('modules')
        else:
            download_dict = {
                'save_path': product_info.get('save_path'),
                'data': product_info.get('data')
            }

            download_dict['data']['modules'] = [module for module in product_info['data']['modules'] if module.get('selected', False)]
            for module in download_dict['data']['modules']:
                lessons = [lesson for lesson in module['lessons'] if lesson.get('selected', False)]
                if not lessons:
                    continue
                else:
                    module['lessons'] = lessons

        self.downloadable_products.append(download_dict)
