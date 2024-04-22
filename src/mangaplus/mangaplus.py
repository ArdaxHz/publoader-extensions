import asyncio
import logging
import math
import re
import string
import traceback
from copy import copy
from datetime import datetime, time, timezone
from pathlib import Path
from typing import List, Optional, Union

import aiohttp
import requests

from publoader.extensions.src.mangaplus import response_updates_pb2 as response_pb
from publoader.extensions.src.mangaplus.response_chapter_pb2 import Response
from publoader.models.dataclasses import Chapter, Manga
from publoader.utils.logs import setup_extension_logs
from publoader.utils.misc import create_new_event_loop, find_key_from_list_value
from publoader.utils.utils import (
    chapter_number_regex,
    open_manga_id_map,
    open_title_regex,
)

__version__ = "0.1.31"

setup_extension_logs(
    logger_name="mangaplus",
    logger_filename="mangaplus",
)

logger = logging.getLogger("mangaplus")


class Extension:
    def __init__(self, extension_dirpath: Path, **kwargs):
        self.name = "mangaplus"
        self.mangadex_group_id = "4f1de6a2-f0c5-4ac5-bce5-02c7dbb67deb"
        self.manga_id_map_filename = "manga_id_map.json"
        self.override_options_filename = "override_options.json"
        self.extension_dirpath = extension_dirpath

        self.fetch_all_chapters = False
        self._posted_chapters_ids = []
        self._updated_chapters: List[Chapter] = []
        self._all_mplus_chapters: List[Chapter] = []
        self._untracked_manga: List[Manga] = []
        self._mplus_base_api_url = "https://jumpg-webapi.tokyo-cdn.com"
        self._chapter_url_format = "https://mangaplus.shueisha.co.jp/viewer/{}"
        self._manga_url_format = "https://mangaplus.shueisha.co.jp/titles/{}"
        self._images_api_url = "https://jumpg-webapi.tokyo-cdn.com/api/manga_viewer?chapter_id={}&split=no&img_quality=super_high"

    @property
    def extension_languages_map(self):
        return {
            "0": "en",
            "1": "es",
            "2": "fr",
            "3": "id",
            "4": "pt-br",
            "5": "ru",
            "6": "th",
            "7": "de",
            "9": "vi",
        }

    @property
    def extension_languages(self) -> List[str]:
        return list(self.extension_languages_map.values())

    @property
    def disabled(self):
        return False

    def get_updated_chapters(self) -> List[Chapter]:
        return self._updated_chapters

    def get_all_chapters(self) -> List[Chapter]:
        return self._all_mplus_chapters

    def get_updated_manga(self) -> List[Manga]:
        return self._untracked_manga

    def update_external_data(
        self, posted_chapter_ids: List[str], fetch_all_chapters: bool, **kwargs
    ) -> None:
        self._posted_chapters_ids = posted_chapter_ids
        self.fetch_all_chapters = fetch_all_chapters

        self.fetch_updates()

    def run_at(self) -> time:
        return time(hour=15, minute=1, tzinfo=timezone.utc)

    def clean_at(self) -> Optional[list]:
        return [2]

    def daily_check_run(self) -> bool:
        return True

    def fetch_updates(self):
        self._manga_id_map = self._open_manga_id_map()
        self.tracked_mangadex_ids = list(self._manga_id_map.keys())
        self.tracked_manga = [
            mplus_id
            for md_id in self._manga_id_map
            for mplus_id in self._manga_id_map[md_id]
        ]
        self.override_options = self._open_override_options()
        self._num2words: Optional[str] = self._get_num2words_string()

        self._get_mplus_updated_manga()
        self._get_mplus_updates()

    def _open_manga_id_map(self):
        return open_manga_id_map(
            self.extension_dirpath.joinpath(self.manga_id_map_filename)
        )

    def _open_override_options(self):
        return open_title_regex(
            self.extension_dirpath.joinpath(self.override_options_filename)
        )

    def _get_language(self, manga_id: str, language: str):
        manga_id = str(manga_id)

        if manga_id in self.override_options.get("custom_language", {}):
            return self.override_options["custom_language"][manga_id]

        language = str(language)
        if language in self.extension_languages:
            return language

        if language in self.extension_languages_map.keys():
            return self.extension_languages_map.get(language, "NULL")

        return "NULL"

    def _get_num2words_string(self):
        num2words_list = self.override_options.get("num2words")
        if num2words_list is None:
            return

        return "(" + "|".join(self.override_options.get("num2words")) + ")"

    def _get_proto_response(self, response_proto: bytes) -> response_pb.Response:
        """Convert api response into readable data."""
        response = response_pb.Response()
        response.ParseFromString(response_proto)
        return response

    async def _request_from_api(
        self, manga_id: Optional[int] = None, updated: Optional[bool] = False
    ) -> Optional[bytes]:
        """Get manga and chapter details from the api."""
        async with aiohttp.ClientSession() as session:
            try:
                if manga_id is not None:
                    url = "/api/title_detail"
                    params = {"title_id": manga_id}
                elif updated:
                    url = "/api/title_list/updated"
                    params = {}

                async with session.get(
                    self._mplus_base_api_url + url,
                    params=params,
                ) as response:
                    return await response.read()
            except Exception as e:
                logger.error(f"{e}: Couldn't get details from the mangaplus api.")
                print("Request API Error", e)
                return

    def _get_mplus_updated_manga(self):
        """Find new untracked mangaplus series."""
        logger.info("Looking for new untracked manga.")
        print("Getting new manga.")

        loop = create_new_event_loop()
        task = self._request_from_api(updated=True)
        updated_manga_response = loop.run_until_complete(task)

        if updated_manga_response is not None:
            updated_manga_response_parsed = self._get_proto_response(
                updated_manga_response
            )
            updated_manga_details = updated_manga_response_parsed.success.updated

            for manga in updated_manga_details.updated_manga_detail:
                if str(manga.updated_manga.manga_id) not in self.tracked_manga:
                    manga_id = str(manga.updated_manga.manga_id)
                    manga_name = manga.updated_manga.manga_name
                    language = self._get_language(
                        manga_id, manga.updated_manga.language
                    )

                    self._untracked_manga.append(
                        Manga(
                            manga_id=manga_id,
                            manga_name=manga_name,
                            manga_language=language,
                            manga_url=self._manga_url_format.format(manga_id),
                        )
                    )
        else:
            logger.error(f"Couldn't get the untracked manga.")

    def _get_mplus_updates(self):
        """Get latest chapter updates."""
        logger.info("Looking for tracked manga new chapters.")
        print("Getting new chapters.")
        tasks = []

        spliced_manga = [
            self.tracked_manga[elem : elem + 3]
            for elem in range(0, len(self.tracked_manga), 3)
        ]

        loop = create_new_event_loop()
        for mangas in spliced_manga:
            task = self._chapter_updates(mangas)
            tasks.append(task)

        loop.run_until_complete(asyncio.gather(*tasks))

    def _decrypt_image(self, url: str, encryption_hex: str) -> bytes:
        """Decrypt the image so it can be saved.
        Args:
            url (str): The image link.
            encryption_hex (str): The key to decrypt the image.
        Returns:
            bytearray: The image data.
        """
        res = requests.get(url)
        data = bytearray(res.content)
        key = bytes.fromhex(encryption_hex)
        a = len(key)
        for s in range(len(data)):
            data[s] ^= key[s % a]
        return bytes(data)

    def _fetch_chapter_images(self, chapter_id):
        """Fetch the images."""
        if self.fetch_all_chapters:
            return

        try:
            response = requests.get(self._images_api_url.format(chapter_id))
        except requests.RequestException as e:
            traceback.print_exc()
            logger.exception(f"Error fetching images data for chapter {chapter_id}.")

        viewer = Response.FromString(response.content).success.manga_viewer
        pages = [p.manga_page for p in viewer.pages if p.manga_page.image_url]
        images = []

        logger.debug(f"{len(pages)} images for chapter {chapter_id}.")

        for page in pages:
            try:
                image = self._decrypt_image(page.image_url, page.encryption_key)
                if image is not None:
                    images.append(image)
            except requests.RequestException as e:
                traceback.print_exc()
                logger.exception(f"Error fetching image data for chapter {chapter_id}.")
                break

        if len(pages) == len(images):
            return images
        return

    def _normalise_chapter_object(
        self, chapter_list, manga_object: Manga
    ) -> List[Chapter]:
        """Return a list of chapter objects made from the api chapter lists."""
        return [
            Chapter(
                chapter_id=mplus_chapter.chapter_id,
                chapter_url=self._chapter_url_format.format(mplus_chapter.chapter_id),
                chapter_timestamp=datetime.fromtimestamp(mplus_chapter.start_timestamp),
                chapter_title=mplus_chapter.chapter_name,
                chapter_expire=datetime.fromtimestamp(mplus_chapter.end_timestamp),
                chapter_number=mplus_chapter.chapter_number,
                chapter_language=self._get_language(
                    manga_object.manga_id, manga_object.manga_language
                ),
                manga_id=manga_object.manga_id,
                md_manga_id=find_key_from_list_value(
                    self._manga_id_map, manga_object.manga_id
                ),
                manga_name=manga_object.manga_name,
                manga_url=self._manga_url_format.format(manga_object.manga_id),
                extension_name=self.name,
            )
            for mplus_chapter in chapter_list
        ]

    async def _chapter_updates(self, mangas: list):
        """Get the updated chapters from each manga."""
        for manga in mangas:
            manga_response = await self._request_from_api(manga_id=manga)
            if manga_response is None:
                continue

            manga_response_parsed = self._get_proto_response(manga_response)

            manga_chapters = manga_response_parsed.success.manga_detail
            manga_object = Manga(
                manga_id=manga_chapters.manga.manga_id,
                manga_name=manga_chapters.manga.manga_name,
                manga_language=self._get_language(
                    manga_chapters.manga.manga_id, manga_chapters.manga.language
                ),
                manga_url=self._manga_url_format.format(manga_chapters.manga.manga_id),
            )

            manga_chapters_lists = []
            manga_chapters_lists.append(
                self._normalise_chapter_object(
                    list(manga_chapters.first_chapter_list), manga_object
                )
            )

            if len(manga_chapters.last_chapter_list) > 0:
                manga_chapters_lists.append(
                    self._normalise_chapter_object(
                        list(manga_chapters.last_chapter_list), manga_object
                    )
                )

            normalised_chapters = self.normalise_chapter_fields(manga_chapters_lists)
            self._all_mplus_chapters.extend(normalised_chapters)

            updated_chapters = [
                chapter
                for chapter in normalised_chapters
                if str(chapter.chapter_id) not in self._posted_chapters_ids
                and chapter.chapter_expire >= datetime.now()
            ]

            if updated_chapters:
                logger.info(f"MangaPlus newly updated chapters: {updated_chapters}")

            self._updated_chapters.extend(updated_chapters)

    def _get_surrounding_chapter(
        self,
        chapters: List[Chapter],
        current_chapter: Chapter,
        next_chapter_search: bool = False,
    ) -> Optional[Chapter]:
        """Find the chapter before or after the current."""
        # Starts from the first chapter before the current
        index_search = reversed(chapters[: chapters.index(current_chapter)])
        if next_chapter_search:
            # Starts from the first chapter after the current
            index_search = chapters[chapters.index(current_chapter) :]

        for chapter in index_search:
            number_match = re.match(
                pattern=r"^#?(\d+)", string=chapter.chapter_number, flags=re.I
            )

            if bool(number_match):
                number = number_match.group(1)
            else:
                number = re.split(
                    r"[\s{}]+".format(re.escape(string.punctuation)),
                    chapter.chapter_number.strip("#"),
                )[0]

            try:
                int(number)
            except ValueError:
                continue
            else:
                return chapter

    def _strip_chapter_number(self, number: Union[str, int]) -> str:
        """Returns the chapter number without the un-needed # or 0."""
        stripped = str(number).strip().strip("#")

        parts = re.split(r"\.|\-", stripped)
        parts[0] = "0" if len(parts[0].lstrip("0")) == 0 else parts[0].lstrip("0")
        stripped = ".".join(parts)

        return stripped

    def _normalise_chapter_number(
        self, chapters: List[Chapter], chapter: Chapter
    ) -> List[Optional[str]]:
        """Rid the extra data from the chapter number for use in ManagDex."""
        current_number = self._strip_chapter_number(chapter.chapter_number)
        chapter_number = chapter.chapter_number

        if chapter_number is not None:
            chapter_number = current_number

        if chapter_number == "ex":
            # Get previous chapter's number for chapter number
            previous_chapter = self._get_surrounding_chapter(chapters, chapter)
            next_chapter = self._get_surrounding_chapter(
                chapters, chapter, next_chapter_search=True
            )

            next_chapter_number = None
            if next_chapter is not None:
                next_chapter_number = self._strip_chapter_number(
                    next_chapter.chapter_number
                )
                next_chapter_number = (
                    int(re.split(r"\.|\-|\,", next_chapter_number)[0]) - 1
                )

            previous_chapter_number = None
            if previous_chapter is not None:
                previous_chapter_number = self._strip_chapter_number(
                    previous_chapter.chapter_number
                )
                if "," in previous_chapter_number:
                    previous_chapter_number = previous_chapter_number.split(",")[-1]
                else:
                    previous_chapter_number = re.split(
                        r"\.|\-", previous_chapter_number
                    )[0]

            if previous_chapter is None:
                # Previous chapter isn't available, use next chapter's number
                # if available
                if next_chapter is None:
                    chapter_number = None
                else:
                    chapter_number = next_chapter_number
                    first_index = next_chapter
                    second_index = chapter
            else:
                chapter_number = previous_chapter_number
                first_index = chapter
                second_index = previous_chapter

            if chapter_number == "ex":
                chapter_number = None

            if chapter_number is not None and current_number != "ex":
                # If difference between current chapter and previous/next
                # chapter is more than 5, use None as chapter_number
                if math.sqrt((int(current_number) - int(chapter_number)) ** 2) >= 5:
                    chapter_number = None

            if chapter_number is not None:
                chapter_decimal = "5"

                # There may be multiple extra chapters before the last numbered chapter
                # Use index difference as decimal to avoid not uploading
                # non-dupes
                try:
                    chapter_difference = chapters.index(first_index) - chapters.index(
                        second_index
                    )
                    if next_chapter is None:
                        chapter_decimal = chapter_difference

                    if chapter_difference > 1:
                        second_index_number = second_index.chapter_number
                        if "." in second_index_number:
                            try:
                                second_index_decimal = int(
                                    second_index_number.rsplit(".")[-1]
                                )
                            except ValueError:
                                pass
                            else:
                                chapter_decimal = second_index_decimal + 1
                        else:
                            chapter_decimal = chapter_difference
                except (ValueError, IndexError):
                    pass

                chapter_number = f"{chapter_number}.{chapter_decimal}"
        elif chapter_number is not None and chapter_number.lower() in (
            "one-shot",
            "one.shot",
        ):
            chapter_number = None
        elif chapter_number is not None and chapter_number.lower().startswith(
            ("spin-off", "spin.off")
        ):
            chapter_number = re.sub(
                r"(?:spin\-off|spin\.off)\s?", "", chapter_number.lower(), re.I
            ).strip()

        if chapter_number is None:
            chapter_number_split = [chapter_number]
        else:
            chapter_number_split = [
                self._strip_chapter_number(chap_number)
                for chap_number in chapter_number.split(",")
            ]

        returned_chapter_numbers = []
        for num in chapter_number_split:
            if num is None or not bool(chapter_number_regex.match(num)):
                returned_chapter_numbers.append(None)
            else:
                returned_chapter_numbers.append(num)

        if (
            chapter.chapter_id
            in self.override_options.get("override_chapter_numbers", {}).keys()
        ):
            returned_chapter_numbers = [
                self.override_options.get("override_chapter_numbers", {}).get(
                    chapter.chapter_id, *returned_chapter_numbers
                )
            ]
        elif (
            chapter.chapter_id in self.override_options.get("multi_chapters", {}).keys()
        ):
            returned_chapter_numbers = self.override_options.get(
                "multi_chapters", {}
            ).get(chapter.chapter_id, returned_chapter_numbers)

        return returned_chapter_numbers

    def _normalise_chapter_title(
        self, chapter: Chapter, chapter_number: List[Optional[str]]
    ) -> Optional[str]:
        """Strip away the title prefix."""
        colon_regex = re.compile(
            r"^(?:\S+\s?)?\d+(?:(?:[\,\-\.])\d{0,2})?\s?[\:]\s?", re.I
        )
        no_title_regex = re.compile(r"^\S+\s?\d+(?:(?:[\,\-\.])\d{0,2})?$", re.I)
        hashtag_regex = re.compile(r"^(?:\S+\s?)?#\d+(?:(?:[\,\-\.])\d{0,2})?\s?", re.I)
        period_dash_regex = re.compile(
            r"^(?:\S+\s?)?\d+(?:(?:[\,\-\.])\d{0,2})?\s?[\.\/\-]\s?", re.I
        )
        spaces_regex = re.compile(r"^(?:\S+\s?)?\d+(?:(?:[\,\-\.])\d{0,2})?\s?", re.I)
        final_chapter_regex = re.compile(
            r"^(?:final|last)\s?(?:chapter|ep|episode)\s?[\:\.]\s?", re.I
        )
        word_numbers_regex = None
        if self._num2words is not None:
            word_numbers_regex = re.compile(
                rf"^(?:\S+\s?)\s?{self._num2words}\s?(?:{self._num2words}\s?)?\:\s?",
                re.I,
            )

        original_title = str(chapter.chapter_title).strip()
        normalised_title = original_title
        pattern_to_use: Optional[re.Pattern[str]] = None
        replace_string = ""
        custom_regex = None

        if (
            chapter.manga_id in self.override_options.get("empty", [])
            and None not in chapter_number
            or original_title.lower() in ("final chapter",)
        ):
            normalised_title = None
            custom_regex = "Empty Title"
        elif chapter.manga_id in self.override_options.get("noformat", []):
            normalised_title = original_title
            custom_regex = "Original Title"
        elif str(chapter.manga_id) in self.override_options.get("custom", {}):
            pattern_to_use = re.compile(
                self.override_options["custom"][str(chapter.manga_id)], re.I
            )
            custom_regex = "Custom Regex"
        elif final_chapter_regex.match(original_title):
            pattern_to_use = final_chapter_regex
            custom_regex = "Final Chapter Regex"
        elif word_numbers_regex is not None and word_numbers_regex.match(
            original_title
        ):
            pattern_to_use = word_numbers_regex
            custom_regex = "Word Numbers Regex"
        elif colon_regex.match(original_title):
            pattern_to_use = colon_regex
        elif no_title_regex.match(original_title):
            pattern_to_use = no_title_regex
        elif period_dash_regex.match(original_title):
            pattern_to_use = period_dash_regex
        elif hashtag_regex.match(original_title):
            pattern_to_use = hashtag_regex
        elif spaces_regex.match(original_title):
            pattern_to_use = spaces_regex

        if pattern_to_use is not None:
            normalised_title = pattern_to_use.sub(
                repl=replace_string, string=original_title, count=1
            ).strip()

        if normalised_title is not None and normalised_title.lower() in (
            "",
            "none",
            "null",
        ):
            normalised_title = None

        # logger.debug(
        #     f"Chapter title normaliser chapter_id: {chapter.chapter_id}, manga_id: {chapter.manga_id}, {custom_regex=}, regex used: {pattern_to_use!r}, {original_title=}, {normalised_title=}"
        # )
        return normalised_title

    def normalise_chapter_fields(
        self, manga_chapters_lists: List[List[Chapter]]
    ) -> List[Chapter]:
        """Normalise the chapter fields for MangaDex."""
        updated_chapters = []

        for chapters in manga_chapters_lists:
            # Go through the last three chapters
            for chapter in chapters:
                chapter_number_split = self._normalise_chapter_number(chapters, chapter)
                chapter_title = self._normalise_chapter_title(
                    chapter, chapter_number_split
                )

                # MPlus sometimes joins two chapters as one, upload to md as
                # two different chapters
                for chap_number in chapter_number_split:
                    copied_chapter = copy(chapter)
                    copied_chapter.chapter_number = chap_number
                    copied_chapter.chapter_title = chapter_title
                    updated_chapters.append(copied_chapter)

        return updated_chapters
