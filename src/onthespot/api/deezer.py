import re
import json
import html.parser
import uuid
import requests
from Cryptodome.Hash import MD5
from Cryptodome.Cipher import AES, Blowfish
from binascii import a2b_hex, b2a_hex
from ..otsconfig import config
from ..runtimedata import get_logger, account_pool
from ..utils import conv_list_format, make_call

logger = get_logger("spotify.api")

DEEZER_BASE = "https://api.deezer.com/"

class ScriptExtractor(html.parser.HTMLParser):
    """ extract <script> tag contents from a html page """
    def __init__(self):
        html.parser.HTMLParser.__init__(self)
        self.scripts = []
        self.curtag = None

    def handle_starttag(self, tag, attrs):
        self.curtag = tag.lower()

    def handle_data(self, data):
        if self.curtag == "script":
            self.scripts.append(data)

    def handle_endtag(self, tag):
        self.curtag = None

def deezer_add_account(arl):
    cfg_copy = config.get('accounts').copy()
    new_user = {
        "uuid": str(uuid.uuid4()),
        "service": "deezer",
        "active": True,
        "login": {
            "arl": arl,
        }
    }
    cfg_copy.append(new_user)
    config.set_('accounts', cfg_copy)
    config.update()

def deezer_get_album_items(album_id):
    album_data = make_call(f"{DEEZER_BASE}/album/{album_id}")
    return album_data.get("tracks", {}).get("data", '')

def deezer_get_playlist_items(playlist_id):
    album_data = make_call(f"{DEEZER_BASE}/playlist/{playlist_id}")
    return album_data.get("tracks", {}).get("data", '')

def deezer_get_playlist_data(playlist_id):
    playlist_data = make_call(f"{DEEZER_BASE}/playlist/{playlist_id}")
    return playlist_data.get("title", ''), playlist_data.get("creator", {}).get("name", '')


def deezer_get_artist_albums(artist_id):
    album_data = make_call(f"{DEEZER_BASE}/artist/{artist_id}/albums")

    url_list = []
    for album in album_data.get("data", ''):
        url_list.append(album.get("link", ''))
    return url_list

def deezer_get_track_metadata(token, item_id):
    logger.info(f"Get track info for: '{item_id}'")
 
    track_data = make_call(f"{DEEZER_BASE}/track/{item_id}")
    album_data = make_call(f"{DEEZER_BASE}/album/{track_data.get('album', {}).get('id', '')}")
    info = {}

    # Initialize an empty list to store contributor names  
    artists = []

    # Append all contributor names to the list  
    for artist in track_data.get('contributors', ''):
        artists.append(artist['name'])

    info['title'] = track_data.get('title', '')
    info['isrc'] = track_data.get('isrc', '')
    info['item_url'] = track_data.get('link', '')
    info['length'] = str(track_data.get('duration', '')) + '000'
    info['track_number'] = track_data.get('track_position', '')
    info['total_tracks'] = len(album_data.get("tracks", {}).get("data", ''))

    # Since you can receive the disc the track is on but not
    # the total number of discs I opted to comment this.
    #info['disc_number'] = track_data.get('disk_number', '')
    #info['total_discs'] = ''

    info['release_year'] = track_data.get('release_date', '').split('-')[0]
    info['explicit'] = track_data.get('explicit_lyrics', '')
    info['bpm'] = track_data.get('bpm', '')
    info['artists'] = conv_list_format(artists)
    info['image_url'] = track_data.get('album', {}).get('cover_xl', '')
    info['album_artists'] = track_data.get('artist', {}).get('name', '')
    info['album_name'] = track_data.get('album', {}).get('title', '')
    info['is_playable'] = track_data.get('readable', '')

    return info

def get_song_infos_from_deezer_website(id):
    url = f"https://www.deezer.com/us/track/{id}"
    session = account_pool[config.get('parsing_acc_sn')]['login']['session']
    resp = session.get(url)
    if resp.status_code == 404:
        logger.info(f'Received 404 while fetching MD5_ORIGIN, {url}')
    if "MD5_ORIGIN" not in resp.text:
        logger.info(f'Deezer MD5_ORIGIN missing for {url}')
    parser = ScriptExtractor()
    parser.feed(resp.text)
    parser.close()

    songs = []
    for script in parser.scripts:
        regex = re.search(r'{"DATA":.*', script)
        if regex:
            DZR_APP_STATE = json.loads(regex.group())
            songs.append(DZR_APP_STATE['DATA'])
    return songs[0]

def md5hex(data):
    """ return hex string of md5 of the given string """
    # type(data): bytes
    # returns: bytes
    h = MD5.new()
    h.update(data)
    return b2a_hex(h.digest())

def hexaescrypt(data, key):
    """ returns hex string of aes encrypted data """
    c = AES.new(key.encode(), AES.MODE_ECB)
    return b2a_hex(c.encrypt(data))

def calcbfkey(songid):
    """ Calculate the Blowfish decrypt key for a given songid """
    key = b"g4el58wc0zvf9na1"
    songid_md5 = md5hex(songid.encode())

    xor_op = lambda i: chr(songid_md5[i] ^ songid_md5[i + 16] ^ key[i])
    decrypt_key = "".join([xor_op(i) for i in range(16)])
    return decrypt_key


def blowfishDecrypt(data, key):
    iv = a2b_hex("0001020304050607")
    c = Blowfish.new(key.encode(), Blowfish.MODE_CBC, iv)
    return c.decrypt(data)

def decryptfile(fh, key, fo):
    """
    Decrypt data from file <fh>, and write to file <fo>.
    decrypt using blowfish with <key>.
    Only every third 2048 byte block is encrypted.
    """
    blockSize = 2048
    i = 0

    for data in fh.iter_content(blockSize):
        if not data:
            break

        isEncrypted = ((i % 3) == 0)
        isWholeBlock = len(data) == blockSize

        if isEncrypted and isWholeBlock:
            data = blowfishDecrypt(data, key)

        fo.write(data)
        i += 1

def genurlkey(songid, md5origin, mediaver=4, fmt=1):
    """ Calculate the deezer download url given the songid, origin and media+format """
    data_concat = b'\xa4'.join(_ for _ in [md5origin.encode(),
                                           str(fmt).encode(),
                                           str(songid).encode(),
                                           str(mediaver).encode()])
    data = b'\xa4'.join([md5hex(data_concat), data_concat]) + b'\xa4'
    if len(data) % 16 != 0:
        data += b'\0' * (16 - len(data) % 16)
    return hexaescrypt(data, "jo6aey6haid2Teih")

def deezer_login_user(account):
    uuid = account['uuid']
    arl = account['login']['arl']
    headers = {
        'Pragma': 'no-cache',
        'Origin': 'https://www.deezer.com',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/68.0.3440.106 Safari/537.36',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': '*/*',
        'Cache-Control': 'no-cache',
        'X-Requested-With': 'XMLHttpRequest',
        'Connection': 'keep-alive',
        'Referer': 'https://www.deezer.com/login',
        'DNT': '1',
    }
    session = requests.Session()
    session.headers.update(headers)
    session.cookies.update({'arl': arl, 'comeback': '1'})

    api_token = None  

    # Prepare to call the API  
    method = 'deezer.getUserData'  
    args = {}  
    params = {}  

    # Main API call logic  

    p = {  
        'api_version': "1.0",  
        'api_token': 'null',
        'input': '3',  
        'method': method  
    }  
    p.update(params)  
    

    resp = session.post(  
        "http://www.deezer.com/ajax/gw-light.php",  
        params=p,  
        timeout=30,  
        json=args,  
        headers=headers
    )

    user_data = resp.json()

    bitrate = '128k'
    if user_data["results"]["USER"]["OPTIONS"]["web_lossless"]:
        bitrate = '1411k'
    elif user_data["results"]["USER"]["OPTIONS"]["web_hq"]:
        bitrate = '320k'

    try:
        account_pool.append({
            "uuid": uuid,
            "username": arl,
            "service": "deezer",
            "status": "active",
            "account_type": "premium" if user_data["results"]["USER"]["OPTIONS"]["web_lossless"] else "free",
            "bitrate": bitrate,
            "login": {
                "arl": arl,
                "license_token": user_data["results"]["USER"]["OPTIONS"]["license_token"],
                "session": session
            }
        })
        return True
    except ConnectionRefusedError:
        account_pool.append({
            "uuid": uuid,
            "username": username,
            "service": "deezer",
            "status": "error",
            "account_type": "N/A",
            "bitrate": "N/A",
            "login": {
                "arl": "",
                "license_token": "",
                "session": "",
            }
        })
        return False

def deezer_get_token(parsing_index):
    return account_pool[config.get('parsing_acc_sn')]['login']['session']

def deezer_get_search_results(token, search_term, content_types):
    params = {}
    params["q"] = search_term
    params["limit"] = config.get("max_search_results")

    album_url = f"{DEEZER_BASE}/search/album"
    artist_url = f"{DEEZER_BASE}/search/artist"
    playlist_url = f"{DEEZER_BASE}/search/playlist"
    track_url = f"{DEEZER_BASE}/search/track"

    album_search = requests.get(album_url, params=params).json()
    artist_search = requests.get(artist_url, params=params).json()
    playlist_search = requests.get(playlist_url, params=params).json()
    track_search = requests.get(track_url, params=params).json()

    search_results = []
    for album in album_search['data']:
        search_results.append({
            'item_id': album['id'],
            'item_name': album['title'],
            'item_by': album['artist']['name'],
            'item_type': "album",
            'item_service': "deezer",
            'item_url': album['link'],
            'item_thumbnail_url': album["cover"]
        })
    for artist in artist_search['data']:
        search_results.append({
            'item_id': artist['id'],
            'item_name': artist['name'],
            'item_by': artist['name'],
            'item_type': "artist",
            'item_service': "deezer",
            'item_url': artist['link'],
            'item_thumbnail_url': artist["picture"]
        })
    for playlist in playlist_search['data']:
        search_results.append({
            'item_id': playlist['id'],
            'item_name': playlist['title'],
            'item_by': playlist['user']['name'],
            'item_type': "playlist",
            'item_service': "deezer",
            'item_url': playlist['link'],
            'item_thumbnail_url': playlist["picture"]
        })
    for track in track_search['data']:
        search_results.append({
            'item_id': track['id'],
            'item_name': track['title'],
            'item_by': track['artist']['name'],
            'item_type': "track",
            'item_service': "deezer",
            'item_url': track['link'],
            'item_thumbnail_url': track["album"]["cover"]
        })


    logger.info(search_results)
    return search_results
