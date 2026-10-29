"""登录总入口：按学校名查campus_host/login_url/login_type并选CAS/IAP。"""
import os
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry
from actions.casLogin import casLogin
from actions.iapLogin import iapLogin
from actions.utils import Utils

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# (连接超时, 读取超时) 秒；服务器在境外时可用环境变量 JRXY_HTTP_TIMEOUT 调大读取超时
HTTP_TIMEOUT = (10, int(os.getenv("JRXY_HTTP_TIMEOUT", "30")))


class TimeoutSession(requests.Session):
    """所有请求默认带超时，避免某个请求无限挂起、把整个签到进程拖到被强制结束。"""

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = HTTP_TIMEOUT
        return super().request(method, url, **kwargs)


def new_session() -> requests.Session:
    s = TimeoutSession()
    # 只重试“连接没建立”的失败（请求肯定没到服务器），不重试读超时，避免重复提交签到
    retry = Retry(total=2, connect=2, read=0, status=0, redirect=0,
                  backoff_factor=0.5, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class wiseLoginService:
    # 初始化本地登录类
    def __init__(self, userInfo, httpProxy):
        if None == userInfo['username'] or '' == userInfo[
                'username'] or None == userInfo['password'] or '' == userInfo[
                    'password'] or None == userInfo[
                        'schoolName'] or '' == userInfo['schoolName']:
            raise Exception('初始化类失败，请键入完整的参数（用户名，密码，学校名称）')
        self.username = userInfo['username']
        self.password = userInfo['password']
        self.schoolName = userInfo['schoolName']
        self.session = new_session()
        headers = {
            'User-Agent':
            'Mozilla/5.0 (Linux; Android 8.0.0; MI 6 Build/OPR1.170623.027; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/92.0.4515.131 Mobile Safari/537.36 okhttp/3.12.4',
        }
        self.session.headers = headers
        if httpProxy != '':
            Utils.log('全局代理已启用')
            self.session.proxies = {'http': httpProxy, 'https': httpProxy}
        self.session.hooks['response'].append(Utils.checkStatus)
        self.login_url = ''
        self.campus_host = ''
        self.login_host = ''
        self.loginEntity = None
        self.login_type = ''

    # 通过学校名称借助api获取学校的登陆url
    def getLoginUrlBySchoolName(self):
        schools = self.session.get(
            'https://mobile.campushoy.com/v6/config/guest/tenant/list',
            verify=False).json()['data']
        flag = False
        for item in schools:
            if item['name'] == self.schoolName:
                flag = True
                if item['joinType'] == 'NONE':
                    raise Exception(self.schoolName + '未加入今日校园，请检查...')
                params = {'ids': item['id']}
                data = self.session.get(
                    'https://mobile.campushoy.com/v6/config/guest/tenant/info',
                    params=params,
                    verify=False,
                ).json()['data'][0]
                joinType = data['joinType']
                ampUrl = data['ampUrl']
                ampUrl2 = data['ampUrl2']
                if 'campusphere' in ampUrl:
                    clientUrl = ampUrl
                elif 'campusphere' in ampUrl2:
                    clientUrl = ampUrl2
                else:
                    raise Exception('未找到客户端登录地址')
                res = self.session.get(clientUrl, verify=False)
                self.campus_host = re.findall('\w{4,5}\:\/\/.*?\/',
                                              clientUrl)[0]
                self.login_url = res.url
                self.login_host = re.findall('\w{4,5}\:\/\/.*?\/', res.url)[0]
                self.login_type = joinType
                break
        if flag == False:
            raise Exception(self.schoolName + '不存在或未加入今日校园')

    # 通过登陆url判断采用哪种登陆方式
    def checkLogin(self):
        if self.login_type == 'CLOUD':
            self.loginEntity = iapLogin(self.username, self.password,
                                        self.login_url, self.login_host,
                                        self.session)
            self.session.cookies = self.loginEntity.login()
        else:
            self.loginEntity = casLogin(self.username, self.password,
                                        self.login_url, self.login_host,
                                        self.session)
            self.session.cookies = self.loginEntity.login()

    # 本地化登陆
    # 按login_type分发casLogin/iapLogin。
    def login(self):
        # 获取学校登陆地址
        self.getLoginUrlBySchoolName()
        self.checkLogin()
