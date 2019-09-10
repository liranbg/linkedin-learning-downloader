import asyncio
import aiohttp
import aiohttp.cookiejar
import lxml.html
import re
import os
import logging

from itertools import chain, filterfalse, starmap
from collections import namedtuple
from urllib.parse import urljoin
from config import USERNAME, PASSWORD, COURSES, PROXY, BASE_DOWNLOAD_PATH

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s')

MAX_DOWNLOADS_SEMAPHORE = asyncio.Semaphore(10)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/66.0.3359.181 Safari/537.36",
    "Accept": "*/*",
}
URL = "https://www.linkedin.com"
FILE_TYPE_VIDEO = ".mp4"
FILE_TYPE_SUBTITLE = ".srt"
COOKIE_JAR = aiohttp.cookiejar.CookieJar()

Course = namedtuple("Course", ["name", "slug", "description", "unlocked", "chapters"])
Chapter = namedtuple("Chapter", ["name", "videos", "index"])
Video = namedtuple("Video", ["name", "slug", "index", "filename"])


def sub_format_time(ms):
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:02}'


def clean_dir_name(dir_name):
    # Remove starting digit and dot (e.g '1. A' -> 'A')
    # Remove bad characters         (e.g 'A: B' -> 'A B')
    no_digit = re.sub(r'^\d+\.', "", dir_name)
    no_bad_chars = re.sub(r'[\\:<>"/|?*]', "", no_digit)
    return no_bad_chars.strip()


def build_course(course_element: dict):
    chapters = [
        Chapter(name=course['title'],
                videos=[
                    Video(name=video['title'],
                          slug=video['slug'],
                          index=idx,
                          filename=f"{str(idx).zfill(2)} - {clean_dir_name(video['title'])}{FILE_TYPE_VIDEO}"
                          )
                    for idx, video in enumerate(course['videos'], start=1)
                ],
                index=idx)
        for idx, course in enumerate(course_element['chapters'], start=1)
    ]
    course = Course(name=course_element['title'],
                    slug=course_element['slug'],
                    description=course_element['description'],
                    unlocked=course_element['fullCourseUnlocked'],
                    chapters=chapters)
    return course


def chapter_dir(course: Course, chapter: Chapter):
    folder_name = f"{str(chapter.index).zfill(2)} - {clean_dir_name(chapter.name)}"
    chapter_path = os.path.join(BASE_DOWNLOAD_PATH, clean_dir_name(course.name), folder_name)
    return chapter_path


async def login(username, password):
    async with aiohttp.ClientSession(headers=HEADERS, cookie_jar=COOKIE_JAR) as session:
        logging.info("[*] Login step 1 - Getting CSRF token...")
        resp = await session.get(URL, proxy=PROXY)
        body = await resp.text()

        # Looking for CSRF Token
        html = lxml.html.fromstring(body)
        csrf = html.xpath("//input[@name='loginCsrfParam']/@value").pop()
        logging.debug(f"[*] CSRF: {csrf}")
        data = {
            "session_key": username,
            "session_password": password,
            "loginCsrfParam": csrf,
            "isJsEnabled": False
        }
        logging.info("[*] Login step 1 - Done")
        logging.info("[*] Login step 2 - Logging In...")
        await session.post(urljoin(URL, 'uas/login-submit'), proxy=PROXY, data=data)

        if not next((x.value for x in session.cookie_jar if x.key.lower() == 'li_at'), False):
            raise RuntimeError("[!] Could not login. Please check your credentials")

        HEADERS['Csrf-Token'] = next(x.value for x in session.cookie_jar if x.key.lower() == 'jsessionid')
        logging.info("[*] Login step 2 - Done")


async def fetch_courses():
    return await asyncio.gather(*map(fetch_course, COURSES))


async def fetch_course(course_slug):
    url = f"{URL}/learning-api/detailedCourses??fields=fullCourseUnlocked,releasedOn,exerciseFileUrls,exerciseFiles&" \
          f"addParagraphsToTranscript=true&courseSlug={course_slug}&q=slugs"

    async with aiohttp.ClientSession(headers=HEADERS, cookie_jar=COOKIE_JAR) as session:
        resp = await session.get(url, proxy=PROXY, headers=HEADERS)
        data = await resp.json()
        course = build_course(data['elements'][0])

        logging.info(f'[*] Fetching course {course.name}')

        await fetch_chapters(course)
        logging.info(f'[*] Finished fetching course "{course.name}"')


async def fetch_chapters(course: Course):
    chapters_dirs = [chapter_dir(course, chapter) for chapter in course.chapters]

    # Creating all missing directories
    missing_directories = filterfalse(os.path.exists, chapters_dirs)
    for d in missing_directories:
        os.makedirs(d)

    await asyncio.gather(*chain.from_iterable(fetch_chapter(course, chapter) for chapter in course.chapters))


def fetch_chapter(course: Course, chapter: Chapter):
    return (
        fetch_video_or_wait(course, chapter, video)
        for video in chapter.videos
    )

async def fetch_video_or_wait(course: Course, chapter: Chapter, video: Video):
    async with MAX_DOWNLOADS_SEMAPHORE:
        await fetch_video(course, chapter, video)

async def fetch_video(course: Course, chapter: Chapter, video: Video):
    subtitles_filename = os.path.splitext(video.filename)[0] + FILE_TYPE_SUBTITLE
    video_file_path = os.path.join(chapter_dir(course, chapter), video.filename)
    subtitle_file_path = os.path.join(chapter_dir(course, chapter), subtitles_filename)
    video_exists = os.path.exists(video_file_path)
    subtitle_exists = os.path.exists(subtitle_file_path)
    if video_exists and subtitle_exists:
        return

    logging.info(f"[~] Fetching course '{course.name}' Chapter no. {chapter.index} Video no. {video.index}")
    async with aiohttp.ClientSession(headers=HEADERS, cookie_jar=COOKIE_JAR) as session:
        video_url = f'{URL}/learning-api/detailedCourses?addParagraphsToTranscript=false&courseSlug={course.slug}&' \
                    f'q=slugs&resolution=_720&videoSlug={video.slug}'
        data = None
        tries = 3
        for _ in range(tries):
            try:
                resp = await session.get(video_url, proxy=PROXY, headers=HEADERS)
                data = await resp.json()
                resp.raise_for_status()
                break
            except aiohttp.client_exceptions.ClientResponseError:
                pass

        video_url = data['elements'][0]['selectedVideo']['url']['progressiveUrl']
        
        try:
            subtitles = data['elements'][0]['selectedVideo']['transcript']
        except Exception as e:
            subtitles = None
        duration_in_ms = int(data['elements'][0]['selectedVideo']['durationInSeconds']) * 1000

        if not video_exists:
            logging.info(f"[~] Writing {video.filename}")
            await download_file(video_url, video_file_path)

        if subtitles is not None:
            logging.info(f"[~] Writing {subtitles_filename}")
            subtitle_lines = subtitles['lines']            
            await write_subtitles(subtitle_lines, subtitle_file_path, duration_in_ms)

    logging.info(f"[~] Done fetching course '{course.name}' Chapter no. {chapter.index} Video no. {video.index}")


async def write_subtitles(subs, output_path, video_duration):
    def subs_to_lines(idx, sub):
        starts_at = sub['transcriptStartAt']
        ends_at = subs[idx]['transcriptStartAt'] if idx < len(subs) else video_duration
        caption = sub['caption']
        return f"{idx}\n" \
               f"{sub_format_time(starts_at)} --> {sub_format_time(ends_at)}\n" \
               f"{caption}\n\n"

    with open(output_path, 'wb') as f:
        for line in starmap(subs_to_lines, enumerate(subs, start=1)):
            f.write(line.encode('utf8'))


async def download_file(url, output):
    async with aiohttp.ClientSession(headers=HEADERS, cookie_jar=COOKIE_JAR) as session:
        async with session.get(url, proxy=PROXY, headers=HEADERS) as r:
            try:
                with open(output, 'wb') as f:
                    while True:
                        chunk = await r.content.read(1024)
                        if not chunk:
                            break
                        f.write(chunk)
            except Exception as e:
                logging.exception(f"[!] Error while downloading: '{e}'")
                if os.path.exists(output):
                    os.remove(output)


async def process():
    try:
        logging.info("[*] -------------Login-------------")
        await login(USERNAME, PASSWORD)
        logging.info("[*] -------------Done-------------")

        logging.info("[*] -------------Fetching Course-------------")
        await fetch_courses()
        logging.info("[*] -------------Done-------------")

    except aiohttp.client_exceptions.ClientProxyConnectionError as e:
        logging.error(f"Proxy Error: {e}")

    except aiohttp.client_exceptions.ClientConnectionError as e:
        logging.error(f"Connection Error: {e}")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(process())
