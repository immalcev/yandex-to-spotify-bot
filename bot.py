from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
import aiohttp
from aiohttp import web
import asyncio
import requests
import json
import re
import os
import math
from datetime import datetime
from dotenv import load_dotenv

# Загружаем секретные ключи из файла .env
load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

BOT_USERNAME = '@yandex_to_spotify_transfer_bot'
DB_FILE = 'users_db.json'
PAGE_SIZE = 10

SPOTIFY_REDIRECT_URI = 'https://t.me/yandex_to_spotify_transfer_bot'
YANDEX_AUTH_URL = "https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d"

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

USER_SESSIONS = {}
TRANSFER_CANCEL_FLAGS = {}
WORKING_ADD_METHOD = None

class Form(StatesGroup):
    waiting_for_yandex_token = State()
    waiting_for_spotify_code = State()

async def safe_delete(message: types.Message):
    try:
        await message.delete()
    except Exception:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_db(db: dict):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_user_data(user_id: int) -> dict:
    db = load_db()
    data = db.get(str(user_id))
    if isinstance(data, str):
        return {"yandex_token": data, "spotify_token": None, "spotify_refresh": None, "track_ids": []}
    return data or {}

def save_user_data(user_id: int, yandex_token: str = None, spotify_token: str = None, spotify_refresh: str = None, track_ids: list = None):
    db = load_db()
    uid_str = str(user_id)
    current = get_user_data(user_id)
    
    db[uid_str] = {
        "yandex_token": yandex_token if yandex_token is not None else current.get("yandex_token"),
        "spotify_token": spotify_token if spotify_token is not None else current.get("spotify_token"),
        "spotify_refresh": spotify_refresh if spotify_refresh is not None else current.get("spotify_refresh"),
        "track_ids": list(dict.fromkeys(track_ids)) if track_ids is not None else current.get("track_ids", [])
    }
    save_db(db)

def add_downloaded_ids(user_id: int, new_ids: list):
    user_data = get_user_data(user_id)
    known_ids = user_data.get("track_ids", [])
    updated_ids = list(dict.fromkeys(known_ids + new_ids))
    save_user_data(user_id, track_ids=updated_ids)

def delete_user_data(user_id: int):
    db = load_db()
    if str(user_id) in db:
        del db[str(user_id)]
        save_db(db)

def fetch_yandex_tracks(token: str, known_ids: list = None, only_new: bool = False):
    headers = {
        'Authorization': f'OAuth {token}',
        'X-Yandex-Music-Client': 'YandexMusicAndroid/24023621'
    }
    try:
        with requests.Session() as s:
            s.headers.update(headers)
            r_status = s.get('https://api.music.yandex.net/account/status', timeout=10)
            if r_status.status_code != 200:
                return None, None, "AUTH_ERROR"
                
            uid = r_status.json().get('result', {}).get('account', {}).get('uid')
            if not uid:
                return None, None, "AUTH_ERROR"

            r_likes = s.get(f'https://api.music.yandex.net/users/{uid}/likes/tracks', timeout=15)
            if r_likes.status_code != 200:
                return None, None, "AUTH_ERROR"

            tracks_raw = r_likes.json().get('result', {}).get('library', {}).get('tracks', [])
            all_track_ids = [str(i['id']) for i in tracks_raw if 'id' in i]
            
            if not all_track_ids:
                return [], [], None

            if only_new and known_ids:
                known_set = set(known_ids)
                target_ids = [tid for tid in all_track_ids if tid not in known_set]
            else:
                target_ids = all_track_ids

            if not target_ids:
                return [], all_track_ids, None

            chunks = [target_ids[i:i+100] for i in range(0, len(target_ids), 100)]
            res_list = []

            for chunk in chunks:
                resp = s.post(
                    'https://api.music.yandex.ru/tracks',
                    data={'track-ids': ','.join(chunk)},
                    timeout=15
                )
                if resp.status_code == 200:
                    for t in resp.json().get('result', []):
                        if t and t.get('artists') and t.get('title'):
                            artist = ', '.join([a['name'] for a in t['artists']])
                            title = t['title']
                            tid = str(t.get('id', ''))
                            res_list.append({'id': tid, 'artist': artist, 'track': title})
                            
            return res_list, all_track_ids, None
    except Exception as e:
        return None, None, f"NETWORK_ERROR: {e}"

def exchange_spotify_code(code: str, redirect_uri: str = SPOTIFY_REDIRECT_URI):
    url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
    }
    auth = (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    response = requests.post(url, data=payload, auth=auth)
    if response.status_code == 200:
        data = response.json()
        return data.get("access_token"), data.get("refresh_token")
    return None, None

def refresh_spotify_access_token(refresh_token: str):
    url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    auth = (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    response = requests.post(url, data=payload, auth=auth)
    if response.status_code == 200:
        return response.json().get("access_token")
    return None

def get_valid_spotify_token(user_id: int):
    user_data = get_user_data(user_id)
    token = user_data.get("spotify_token")
    refresh_token = user_data.get("spotify_refresh")
    
    if not token:
        return None
        
    headers = {"Authorization": f"Bearer {token}"}
    test_resp = requests.get("https://api.spotify.com/v1/me", headers=headers)
    
    if test_resp.status_code == 200:
        return token
        
    if refresh_token:
        new_token = refresh_spotify_access_token(refresh_token)
        if new_token:
            save_user_data(user_id, spotify_token=new_token)
            return new_token
            
    return None

def create_spotify_playlist(spotify_token: str):
    headers = {
        "Authorization": f"Bearer {spotify_token}",
        "Content-Type": "application/json"
    }
    date_str = datetime.now().strftime("%d.%m.%y")
    playlist_name = f"Яндекс.Музыка {date_str}"
    
    playlist_resp = requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers=headers,
        json={
            "name": playlist_name,
            "description": f"Перенесено с помощью Telegram-бота {BOT_USERNAME}",
            "public": True
        }
    )
    
    if playlist_resp.status_code not in (200, 201):
        return None, None, f"PLAYLIST_CREATE_ERROR_{playlist_resp.status_code}_{playlist_resp.text}"
        
    data = playlist_resp.json()
    return data.get("id"), data.get("external_urls", {}).get("spotify"), None

async def fast_search_track(http_session: aiohttp.ClientSession, sem: asyncio.Semaphore, spotify_token: str, t: dict):
    headers = {"Authorization": f"Bearer {spotify_token}"}
    query = f"{t['artist']} {t['track']}"
    async with sem:
        for _ in range(2):
            try:
                async with http_session.get(
                    "https://api.spotify.com/v1/search",
                    headers=headers,
                    params={"q": query, "type": "track", "limit": 1},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.status == 429:
                        retry_after = min(int(r.headers.get("Retry-After", 2)), 5)
                        await asyncio.sleep(retry_after + 0.2)
                        continue
                    if r.status == 200:
                        data = await r.json()
                        items = data.get("tracks", {}).get("items", [])
                        if items:
                            return items[0]["uri"], items[0]["id"]
                    break
            except Exception:
                await asyncio.sleep(0.2)
    return None, None

def add_uris_to_spotify(spotify_token: str, playlist_id: str, uris: list, ids: list):
    global WORKING_ADD_METHOD
    if not uris:
        return True, "playlist", None
        
    headers = {
        "Authorization": f"Bearer {spotify_token}",
        "Content-Type": "application/json"
    }
    
    methods_to_try = [WORKING_ADD_METHOD] if WORKING_ADD_METHOD else ["items", "tracks", "liked_songs"]
    
    for method in methods_to_try:
        if method == "items":
            r = requests.post(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
                headers=headers,
                json={"uris": uris},
                timeout=15
            )
            if r.status_code in (200, 201):
                WORKING_ADD_METHOD = "items"
                return True, "playlist", None
                
        elif method == "tracks":
            r = requests.post(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                headers=headers,
                json={"uris": uris},
                timeout=15
            )
            if r.status_code in (200, 201):
                WORKING_ADD_METHOD = "tracks"
                return True, "playlist", None
                
        elif method == "liked_songs":
            r = requests.put(
                "https://api.spotify.com/v1/me/tracks",
                headers=headers,
                json={"ids": ids[:50]},
                timeout=15
            )
            if r.status_code in (200, 201, 204):
                WORKING_ADD_METHOD = "liked_songs"
                return True, "liked_songs", None

    if WORKING_ADD_METHOD is not None:
        WORKING_ADD_METHOD = None
        return add_uris_to_spotify(spotify_token, playlist_id, uris, ids)

    return False, None, "Не удалось добавить треки в Spotify (ошибка доступа 403)."

def get_yandex_auth_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Войти в Яндекс.Музыку", url=YANDEX_AUTH_URL)]
    ])

def get_main_keyboard(has_history: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if has_history:
        buttons.append([InlineKeyboardButton(text="🆕 Найти новые треки", callback_data="scan_new")])
    buttons.append([InlineKeyboardButton(text="📥 Вся медиатека", callback_data="scan_all")])
    buttons.append([InlineKeyboardButton(text="🟢 Перенести в Spotify", callback_data="start_spotify_transfer")])
    buttons.append([InlineKeyboardButton(text="🔄 Сменить аккаунт (Reset)", callback_data="reset_acc")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Остановить перенос", callback_data="cancel_transfer")]
    ])

def get_transfer_summary_keyboard(transferred_count: int, not_transferred_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Перенесенные ({transferred_count})", callback_data="res_tr_0"),
            InlineKeyboardButton(text=f"❌ Неперенесенные ({not_transferred_count})", callback_data="res_untr_0")
        ],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def build_result_list_view(user_id: int, list_type: str, page: int = 0):
    session = USER_SESSIONS.get(user_id, {})
    tracks = session.get("transferred_tracks", []) if list_type == "tr" else session.get("not_transferred_tracks", [])
    tr_cnt = len(session.get("transferred_tracks", []))
    untr_cnt = len(session.get("not_transferred_tracks", []))

    title_icon = "✅ **Перенесенные песни**" if list_type == "tr" else "❌ **Неперенесенные песни**"
    
    if not tracks:
        text = f"{title_icon}\n\nСписок пуст!"
        return text, get_transfer_summary_keyboard(tr_cnt, untr_cnt)

    total_tracks = len(tracks)
    total_pages = math.ceil(total_tracks / PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_tracks)

    lines = [f"**{idx + 1}.** {tracks[idx]['artist']} — {tracks[idx]['track']}" for idx in range(start_idx, end_idx)]
    text = f"{title_icon} ({total_tracks} шт.):\n\n" + "\n".join(lines)

    kb = []
    if total_pages > 1:
        prev_cb = f"res_{list_type}_{page - 1}" if page > 0 else "noop_left"
        next_cb = f"res_{list_type}_{page + 1}" if page < total_pages - 1 else "noop_right"
        kb.append([
            InlineKeyboardButton(text="⬅️" if page > 0 else "▫️", callback_data=prev_cb),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop_page"),
            InlineKeyboardButton(text="➡️" if page < total_pages - 1 else "▫️", callback_data=next_cb)
        ])

    kb.append([InlineKeyboardButton(text=f"📥 Скачать этот список .txt ({total_tracks})", callback_data=f"dl_res_{list_type}")])
    
    if list_type == "untr" and total_tracks > 0:
        kb.append([InlineKeyboardButton(text=f"🟢 Доперенести эти песни ({total_tracks})", callback_data="retry_untransferred")])

    other_btn = InlineKeyboardButton(text=f"❌ Неперенесенные ({untr_cnt})", callback_data="res_untr_0") if list_type == "tr" else InlineKeyboardButton(text=f"✅ Перенесенные ({tr_cnt})", callback_data="res_tr_0")
    kb.append([other_btn])
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])

    return text, InlineKeyboardMarkup(inline_keyboard=kb)

def build_tracks_view(user_id: int, page: int = 0, select_mode: bool = False):
    session = USER_SESSIONS.get(user_id)
    if not session or not session["tracks"]:
        return "Список треков пуст.", get_main_keyboard(True)

    tracks = session["tracks"]
    selected = session["selected"]
    mode = session["mode"]
    total_tracks = len(tracks)
    total_pages = math.ceil(total_tracks / PAGE_SIZE)
    
    page = max(0, min(page, total_pages - 1))
    start_idx = page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_tracks)

    header = f"🎉 **Новые треки ({total_tracks} шт.):**\n\n" if mode == "new" else f"🎵 **Все треки ({total_tracks} шт.):**\n\n"
    
    lines = []
    for idx in range(start_idx, end_idx):
        t = tracks[idx]
        mark = ("✅ " if idx in selected else "⬜ ") if select_mode else ""
        lines.append(f"{mark}**{idx + 1}.** {t['artist']} — {t['track']}")

    text = header + "\n".join(lines)
    if select_mode:
        text += f"\n\n☑️ *Выбрано для переноса: {len(selected)} из {total_tracks}.*"

    kb = []
    if select_mode:
        for idx in range(start_idx, end_idx):
            t = tracks[idx]
            mark = "✅" if idx in selected else "⬜"
            btn_label = f"{mark} {idx + 1}. {t['artist']} - {t['track']}"
            if len(btn_label) > 55:
                btn_label = btn_label[:52] + "..."
            kb.append([InlineKeyboardButton(text=btn_label, callback_data=f"tog_{idx}_{page}")])

    if total_pages > 1:
        prev_cb = f"page_{page - 1}_{int(select_mode)}" if page > 0 else "noop_left"
        next_cb = f"page_{page + 1}_{int(select_mode)}" if page < total_pages - 1 else "noop_right"
        kb.append([
            InlineKeyboardButton(text="⬅️" if page > 0 else "▫️", callback_data=prev_cb),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop_page"),
            InlineKeyboardButton(text="➡️" if page < total_pages - 1 else "▫️", callback_data=next_cb)
        ])

    if not select_mode:
        kb.append([
            InlineKeyboardButton(text=f"📥 Скачать .txt ({total_tracks})", callback_data="dl_txt_all"),
            InlineKeyboardButton(text="☑️ Выбрать...", callback_data=f"mode_sel_{page}")
        ])
    else:
        kb.append([
            InlineKeyboardButton(text="✅ Выбрать все", callback_data=f"sel_all_{page}"),
            InlineKeyboardButton(text="❌ Сбросить", callback_data=f"sel_clear_{page}")
        ])
        kb.append([
            InlineKeyboardButton(text=f"📥 Скачать выбранные ({len(selected)})", callback_data="dl_txt_selected")
        ])
        kb.append([
            InlineKeyboardButton(text="🔙 К обычному списку", callback_data=f"mode_view_{page}")
        ])

    kb.append([InlineKeyboardButton(text="🟢 Отправить выбранное в Spotify", callback_data="transfer_to_spotify_direct")])
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])

    return text, InlineKeyboardMarkup(inline_keyboard=kb)

async def run_spotify_transfer_task(message: types.Message, user_id: int, valid_spotify_token: str, tracks_to_transfer: list):
    total_to_transfer = len(tracks_to_transfer)
    TRANSFER_CANCEL_FLAGS[user_id] = False
    
    wait_msg = await message.answer(
        f"⚡ Создаю плейлист в Spotify для {total_to_transfer} треков...",
        reply_markup=get_cancel_keyboard()
    )
    
    playlist_id, playlist_url, err = await asyncio.to_thread(create_spotify_playlist, valid_spotify_token)
    if err:
        await wait_msg.edit_text(f"❌ Ошибка создания плейлиста: `{err}`", parse_mode="Markdown")
        return

    transferred_tracks = []
    not_transferred_tracks = []
    pending_uris = []
    pending_ids = []
    pending_track_objs = []
    target_dest = "playlist"

    sem = asyncio.Semaphore(3)
    step_size = 10
    cancelled = False

    async with aiohttp.ClientSession() as http_session:
        for start_i in range(0, total_to_transfer, step_size):
            if TRANSFER_CANCEL_FLAGS.get(user_id):
                cancelled = True
                not_transferred_tracks.extend(tracks_to_transfer[start_i:])
                break

            chunk = tracks_to_transfer[start_i:start_i + step_size]
            tasks = [fast_search_track(http_session, sem, valid_spotify_token, t) for t in chunk]
            results = await asyncio.gather(*tasks)

            for t_obj, (uri, tid) in zip(chunk, results):
                if uri and tid:
                    pending_uris.append(uri)
                    pending_ids.append(tid)
                    pending_track_objs.append(t_obj)
                else:
                    not_transferred_tracks.append(t_obj)

            if len(pending_uris) >= 50 or (start_i + step_size >= total_to_transfer and pending_uris):
                ok, dest_type, add_err = await asyncio.to_thread(
                    add_uris_to_spotify, valid_spotify_token, playlist_id, pending_uris, pending_ids
                )
                if not ok:
                    await wait_msg.edit_text(f"⚠️ Ошибка при добавлении треков: `{add_err}`", parse_mode="Markdown")
                    return
                target_dest = dest_type
                transferred_tracks.extend(pending_track_objs)
                pending_uris.clear()
                pending_ids.clear()
                pending_track_objs.clear()

            processed = min(start_i + step_size, total_to_transfer)
            current_added = len(transferred_tracks) + len(pending_track_objs)
            percent = int((processed / total_to_transfer) * 100)
            last_t = chunk[-1]

            try:
                await wait_msg.edit_text(
                    f"⏳ **Перенос треков в Spotify... ({percent}%)**\n\n"
                    f"🔎 Проверено: **{processed} из {total_to_transfer}**\n"
                    f"✅ Найдено и добавлено: **{current_added}**\n"
                    f"🎵 Сейчас: *{last_t['artist']} — {last_t['track']}*",
                    parse_mode="Markdown",
                    reply_markup=get_cancel_keyboard()
                )
            except Exception:
                pass

    if pending_uris:
        ok, dest_type, _ = await asyncio.to_thread(
            add_uris_to_spotify, valid_spotify_token, playlist_id, pending_uris, pending_ids
        )
        if ok:
            target_dest = dest_type
            transferred_tracks.extend(pending_track_objs)
        else:
            not_transferred_tracks.extend(pending_track_objs)

    session = USER_SESSIONS.get(user_id, {})
    session["transferred_tracks"] = transferred_tracks
    session["not_transferred_tracks"] = not_transferred_tracks
    USER_SESSIONS[user_id] = session

    exported_ids = [t['id'] for t in transferred_tracks if t.get('id')]
    if exported_ids:
        add_downloaded_ids(user_id, exported_ids)

    await safe_delete(wait_msg)

    if target_dest == "liked_songs":
        dest_text = "💜 Сохранено в твои **«Любимые треки» (Liked Songs)** в Spotify!"
    else:
        dest_text = f"🎵 Плейлист: `Яндекс.Музыка {datetime.now().strftime('%d.%m.%y')}`\n🔗 [Открыть плейлист в Spotify]({playlist_url})"

    tr_cnt = len(transferred_tracks)
    untr_cnt = len(not_transferred_tracks)

    status_header = "🛑 **Перенос остановлен!**" if cancelled else "✅ **Готово! Перенос успешно завершен!**"

    await message.answer(
        f"{status_header}\n\n"
        f"✅ Перенесено песен: **{tr_cnt}**\n"
        f"❌ Не перенесено: **{untr_cnt}**\n\n"
        f"{dest_text}",
        parse_mode="Markdown",
        reply_markup=get_transfer_summary_keyboard(tr_cnt, untr_cnt)
    )

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_data = get_user_data(message.from_user.id)
    token = user_data.get("yandex_token")
    known_ids = user_data.get("track_ids", [])

    if token:
        await message.answer(
            f"👋 **С возвращением!** Аккаунт Яндекс.Музыки подключен.\n"
            f"💾 В истории сохранено треков: **{len(known_ids)}**.\n\n"
            "Выбери действие:",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(has_history=bool(known_ids))
        )
    else:
        await message.answer(
            "👋 **Привет! Я помогу быстро перенести лайки из Яндекс.Музыки в Spotify.**\n\n"
            "1. Нажми кнопку **🔑 Войти в Яндекс.Музыку** ниже.\n"
            "2. После входа скопируй ссылку из адресной строки браузера и отправь её сюда в чат!",
            parse_mode="Markdown",
            reply_markup=get_yandex_auth_keyboard()
        )
        await state.set_state(Form.waiting_for_yandex_token)

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    TRANSFER_CANCEL_FLAGS[message.from_user.id] = True
    delete_user_data(message.from_user.id)
    USER_SESSIONS.pop(message.from_user.id, None)
    await message.answer(
        "🔄 **Данные аккаунта сброшены!**\n\n"
        "Нажми кнопку **🔑 Войти в Яндекс.Музыку** ниже и отправь новую ссылку в чат:",
        parse_mode="Markdown",
        reply_markup=get_yandex_auth_keyboard()
    )
    await state.set_state(Form.waiting_for_yandex_token)

@dp.message(Form.waiting_for_yandex_token)
async def process_yandex_token(message: types.Message, state: FSMContext):
    text = message.text.strip()
    match = re.search(r'access_token=([^&]+)', text)
    token = match.group(1) if match else text

    await safe_delete(message)

    save_user_data(message.from_user.id, yandex_token=token, track_ids=[])
    await state.clear()
    await scan_and_show(message, state, message.from_user.id, only_new=False)

async def scan_and_show(message: types.Message, state: FSMContext, user_id: int, only_new: bool = False):
    user_data = get_user_data(user_id)
    token = user_data.get("yandex_token")
    known_ids = user_data.get("track_ids", [])

    wait_msg = await message.answer("⏳ Синхронизируюсь с Яндекс.Музыкой...")
    tracks, all_current_ids, error = await asyncio.to_thread(fetch_yandex_tracks, token, known_ids, only_new)

    if error:
        await safe_delete(wait_msg)
        await message.answer(
            "⚠️ Токен Яндекса устарел. Пожалуйста, войди заново:",
            reply_markup=get_yandex_auth_keyboard()
        )
        await state.set_state(Form.waiting_for_yandex_token)
        return

    if not tracks:
        await wait_msg.edit_text("✨ Новых треков нет!", reply_markup=get_main_keyboard(True))
        return

    if not known_ids and not only_new:
        save_user_data(user_id, yandex_token=token, track_ids=all_current_ids)

    USER_SESSIONS[user_id] = {
        "tracks": tracks,
        "all_ids": all_current_ids,
        "selected": set(range(len(tracks))),
        "mode": "new" if only_new else "all",
        "transferred_tracks": [],
        "not_transferred_tracks": []
    }

    await safe_delete(wait_msg)
    text, markup = build_tracks_view(user_id, page=0, select_mode=False)
    await message.answer(text, parse_mode="Markdown", reply_markup=markup)

@dp.callback_query()
async def handle_callbacks(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data
    user_id = callback.from_user.id

    if data in ("noop_page", "noop_left", "noop_right"):
        await callback.answer("📑")
        return

    if data == "cancel_transfer":
        TRANSFER_CANCEL_FLAGS[user_id] = True
        await callback.answer("🛑 Останавливаю перенос...", show_alert=False)
        return

    if data.startswith("res_tr_") or data.startswith("res_untr_"):
        await callback.answer()
        parts = data.split("_")
        list_type = parts[1]
        page = int(parts[2])
        text, markup = build_result_list_view(user_id, list_type, page)
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data in ("dl_res_tr", "dl_res_untr"):
        session = USER_SESSIONS.get(user_id, {})
        is_tr = (data == "dl_res_tr")
        tracks_to_dl = session.get("transferred_tracks", []) if is_tr else session.get("not_transferred_tracks", [])
        if not tracks_to_dl:
            await callback.answer("Список пуст!", show_alert=True)
            return
        filename = f"{'transferred' if is_tr else 'not_transferred'}_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join([f"{t['artist']} - {t['track']}" for t in tracks_to_dl]))
        await callback.message.answer_document(document=FSInputFile(filename))
        if os.path.exists(filename):
            os.remove(filename)
        await callback.answer("Файл отправлен!")
        return

    if data == "retry_untransferred":
        await callback.answer()
        session = USER_SESSIONS.get(user_id, {})
        untr = session.get("not_transferred_tracks", [])
        if not untr:
            await callback.answer("Нет песен для повторного переноса!", show_alert=True)
            return
        valid_spotify_token = await asyncio.to_thread(get_valid_spotify_token, user_id)
        if not valid_spotify_token:
            await callback.message.answer("⚠️ Токен Spotify устарел, нажми /start.")
            return
        asyncio.create_task(run_spotify_transfer_task(callback.message, user_id, valid_spotify_token, list(untr)))
        return

    if data == "main_menu":
        await callback.answer()
        user_data = get_user_data(user_id)
        await callback.message.edit_text(
            f"🏠 **Главное меню**\n💾 Сохранено в базе: **{len(user_data.get('track_ids', []))}** треков.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(True)
        )
        return

    if data == "reset_acc":
        await callback.answer()
        TRANSFER_CANCEL_FLAGS[user_id] = True
        delete_user_data(user_id)
        USER_SESSIONS.pop(user_id, None)
        await callback.message.edit_text(
            "🔄 **Данные аккаунта сброшены!**\n\n"
            "Нажми кнопку **🔑 Войти в Яндекс.Музыку** ниже и отправь новую ссылку в чат:",
            parse_mode="Markdown",
            reply_markup=get_yandex_auth_keyboard()
        )
        await state.set_state(Form.waiting_for_yandex_token)
        return

    if data == "scan_new":
        await callback.answer()
        await safe_delete(callback.message)
        await scan_and_show(callback.message, state, user_id, only_new=True)
        return
    elif data == "scan_all":
        await callback.answer()
        await safe_delete(callback.message)
        await scan_and_show(callback.message, state, user_id, only_new=False)
        return

    if data in ("start_spotify_transfer", "transfer_to_spotify_direct"):
        await callback.answer()
        valid_spotify_token = await asyncio.to_thread(get_valid_spotify_token, user_id)
        
        if not valid_spotify_token:
            scopes = "playlist-modify-public%20playlist-modify-private%20user-read-private%20user-library-modify"
            spotify_auth_url = (
                f"https://accounts.spotify.com/authorize"
                f"?client_id={SPOTIFY_CLIENT_ID}"
                f"&response_type=code"
                f"&redirect_uri={SPOTIFY_REDIRECT_URI}"
                f"&scope={scopes}"
                f"&state={user_id}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🟢 Войти в Spotify", url=spotify_auth_url)]
            ])
            await callback.message.answer(
                "🟢 **Авторизация в Spotify**\n\n"
                "Нажми на кнопку ниже и подтверди вход в браузере:",
                parse_mode="Markdown",
                reply_markup=kb
            )
            await state.set_state(Form.waiting_for_spotify_code)
            return
        
        session = USER_SESSIONS.get(user_id)
        if not session or not session.get("tracks"):
            wait_ses = await callback.message.answer("🔄 Подтягиваю треки из Яндекс.Музыки для переноса...")
            user_data = get_user_data(user_id)
            token = user_data.get("yandex_token")
            known_ids = user_data.get("track_ids", [])
            
            tracks, all_current_ids, error = await asyncio.to_thread(fetch_yandex_tracks, token, known_ids, False)
            await safe_delete(wait_ses)
            
            if error or not tracks:
                await callback.message.answer("⚠️ Не удалось загрузить треки. Попробуй нажать /start заново.")
                return
                
            session = {
                "tracks": tracks,
                "all_ids": all_current_ids,
                "selected": set(range(len(tracks))),
                "mode": "all",
                "transferred_tracks": [],
                "not_transferred_tracks": []
            }
            USER_SESSIONS[user_id] = session

        tracks_to_transfer = [session["tracks"][i] for i in sorted(session["selected"])] if session.get("selected") else session["tracks"]
        asyncio.create_task(run_spotify_transfer_task(callback.message, user_id, valid_spotify_token, tracks_to_transfer))
        return

    session = USER_SESSIONS.get(user_id)
    if not session:
        await callback.answer("Сессия устарела, нажмите /start", show_alert=True)
        return

    if data.startswith("page_"):
        _, page_str, mode_str = data.split("_")
        await callback.answer()
        text, markup = build_tracks_view(user_id, page=int(page_str), select_mode=bool(int(mode_str)))
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("mode_sel_"):
        xml_data = data.split("_")
        await callback.answer()
        text, markup = build_tracks_view(user_id, page=int(data.split("_")[-1]), select_mode=True)
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return
    elif data.startswith("mode_view_"):
        await callback.answer()
        text, markup = build_tracks_view(user_id, page=int(data.split("_")[-1]), select_mode=False)
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("tog_"):
        _, idx_str, page_str = data.split("_")
        idx, page = int(idx_str), int(page_str)
        if idx in session["selected"]:
            session["selected"].remove(idx)
        else:
            session["selected"].add(idx)
        await callback.answer()
        text, markup = build_tracks_view(user_id, page=page, select_mode=True)
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("sel_all_"):
        session["selected"] = set(range(len(session["tracks"])))
        await callback.answer("Выбраны все")
        text, markup = build_tracks_view(user_id, page=int(data.split("_")[-1]), select_mode=True)
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return
    elif data.startswith("sel_clear_"):
        session["selected"].clear()
        await callback.answer("Сброшено")
        text, markup = build_tracks_view(user_id, page=int(data.split("_")[-1]), select_mode=True)
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data in ("dl_txt_all", "dl_txt_selected"):
        tracks_to_dl = session["tracks"] if data == "dl_txt_all" else [session["tracks"][i] for i in sorted(session["selected"])]
        filename = f"tracks_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join([f"{t['artist']} - {t['track']}" for t in tracks_to_dl]))
        await callback.message.answer_document(document=FSInputFile(filename))
        if os.path.exists(filename):
            os.remove(filename)
        await callback.answer("Файл отправлен!")
        return

@dp.message(Form.waiting_for_spotify_code)
async def process_spotify_auth(message: types.Message, state: FSMContext):
    text = message.text.strip()
    match = re.search(r'code=([^&]+)', text)
    if not match:
        await message.answer("❌ Не удалось найти код авторизации в ссылке. Попробуй еще раз.")
        return

    code = match.group(1)
    wait_msg = await message.answer("🔄 Подключаю Spotify...")
    
    spotify_token, spotify_refresh = await asyncio.to_thread(exchange_spotify_code, code, SPOTIFY_REDIRECT_URI)
    if not spotify_token:
        spotify_token, spotify_refresh = await asyncio.to_thread(
            exchange_spotify_code, code, 'https://t.me/yandex_to_spotify_transfer_bot'
        )
        
    if not spotify_token:
        await wait_msg.edit_text("❌ Ошибка подключения Spotify. Нажми кнопку переноса заново.")
        await state.clear()
        return

    save_user_data(message.from_user.id, spotify_token=spotify_token, spotify_refresh=spotify_refresh)
    await state.clear()
    
    await wait_msg.edit_text(
        "✅ **Spotify успешно подключен!**\n\n"
        "Нажми кнопку **🟢 Перенести в Spotify** ниже, чтобы запустить перенос треков!", 
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(True)
    )

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    print("🚀 Бот запущен! Ожидание сообщений...")
    
    while True:
        try:
            await dp.start_polling(bot, handle_as_tasks=False)
        except Exception as e:
            print(f"⚠️ Ошибка соединения с Telegram: {e}. Переподключение...")
            await asyncio.sleep(3)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")