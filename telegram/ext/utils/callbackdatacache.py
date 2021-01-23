#!/usr/bin/env python
#
# A library that provides a Python interface to the Telegram Bot API
# Copyright (C) 2015-2021
# Leandro Toledo de Souza <devs@python-telegram-bot.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser Public License for more details.
#
# You should have received a copy of the GNU Lesser Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].
"""This module contains the CallbackDataCache class."""
import logging
import time
from datetime import datetime
from threading import Lock
from typing import Dict, Any, Tuple, Union, Optional, MutableMapping
from uuid import uuid4

from cachetools import LRUCache  # pylint: disable=E0401

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, TelegramError, CallbackQuery
from telegram.utils.helpers import to_float_timestamp
from telegram.ext.utils.types import CDCData


class InvalidCallbackData(TelegramError):
    """
    Raised when the received callback data has been tempered with or deleted from cache.

    Args:
        callback_data (:obj:`int`, optional): The button data of which the callback data could not
            be found.
    """

    def __init__(self, callback_data: str = None) -> None:
        super().__init__(
            'The object belonging to this callback_data was deleted or the callback_data was '
            'manipulated.'
        )
        self.callback_data = callback_data

    def __reduce__(self) -> Tuple[type, Tuple[Optional[str]]]:  # type: ignore[override]
        return self.__class__, (self.callback_data,)


class KeyboardData:
    def __init__(
        self, keyboard_uuid: str, access_time: float = None, button_data: Dict[str, Any] = None
    ):
        self.keyboard_uuid = keyboard_uuid
        self.button_data = button_data or {}
        self.access_time = access_time or time.time()

    def update(self) -> None:
        """
        Updates the access time with the current time.
        """
        self.access_time = time.time()

    def to_tuple(self) -> Tuple[str, float, Dict[str, Any]]:
        """
        Gives a tuple representation consisting of keyboard uuid, access time and button data.
        """
        return self.keyboard_uuid, self.access_time, self.button_data


class CallbackDataCache:
    """A custom cache for storing the callback data of a :class:`telegram.ext.Bot.`. Internally, it
    keeps to mappings:

        * One for mapping the data received in callback queries to the cached objects
        * One for mapping the IDs of received callback queries to the cached objects

    If necessary, will drop the least recently used items.

    Args:
        maxsize (:obj:`int`, optional): Maximum number of items in each of the internal mappings.
            Defaults to 1024.
        persistent_data (:obj:`telegram.ext.utils.types.CDCData`, optional): Data to initialize
            the cache with, as returned by :meth:`telegram.ext.BasePersistence.get_callback_data`.

    Attributes:
        maxsize (:obj:`int`): maximum size of the cache.

    """

    def __init__(
        self,
        maxsize: int = 1024,
        persistent_data: CDCData = None,
    ):
        self.logger = logging.getLogger(__name__)

        self.maxsize = maxsize
        self._keyboard_data: MutableMapping[str, KeyboardData] = LRUCache(maxsize=maxsize)
        self._callback_queries: MutableMapping[str, str] = LRUCache(maxsize=maxsize)
        self.__lock = Lock()

        if persistent_data:
            keyboard_data, callback_queries = persistent_data
            for key, value in callback_queries.items():
                self._callback_queries[key] = value
            for uuid, access_time, data in keyboard_data:
                self._keyboard_data[uuid] = KeyboardData(
                    keyboard_uuid=uuid, access_time=access_time, button_data=data
                )

    @property
    def persistence_data(self) -> CDCData:
        """
        The data that needs to be persisted to allow caching callback data across bot reboots.
        """
        # While building a list/dict from the LRUCaches has linear runtime (in the number of
        # entries), the runtime is bounded unless and it has the big upside of not throwing a
        # highly customized data structure at users trying to implement a custom pers class
        with self.__lock:
            return list(data.to_tuple() for data in self._keyboard_data.values()), dict(
                self._callback_queries.items()
            )

    def put_keyboard(self, reply_markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
        """
        Registers the reply markup to the cache. If any of the buttons have :attr:`callback_data`,
        stores that data and builds a new keyboard the the correspondingly replaced buttons.
        Otherwise does nothing and returns the original reply markup.

        Args:
            reply_markup (:class:`telegram.InlineKeyboardMarkup`): The keyboard.

        Returns:
            :class:`telegram.InlineKeyboardMarkup`: The keyboard to be passed to Telegram.

        """
        with self.__lock:
            return self.__put_keyboard(reply_markup)

    @staticmethod
    def __put_button(callback_data: Any, keyboard_data: KeyboardData) -> str:
        """
        Stores the data for a single button in :attr:`keyboard_data`.
        Returns the string that should be passed instead of the callback_data, which is
        ``keyboard_uuid + button_uuids``.
        """
        uuid = uuid4().hex
        keyboard_data.button_data[uuid] = callback_data
        return f'{keyboard_data.keyboard_uuid}{uuid}'

    @staticmethod
    def extract_uuids(callback_data: str) -> Tuple[str, str]:
        """
        Extracts the keyboard uuid and the button uuid form the given ``callback_data``.

        Args:
            callback_data (:obj:`str`): The ``callback_data`` as present in the button.

        Returns:
            (:obj:`str`, :obj:`str`): Tuple of keyboard and button uuid

        """
        # Extract the uuids as put in __put_button
        return callback_data[:32], callback_data[32:]

    def __put_keyboard(self, reply_markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
        keyboard_uuid = uuid4().hex
        keyboard_data = KeyboardData(keyboard_uuid)

        # Built a new nested list of buttons by replacing the callback data if needed
        buttons = [
            [
                # We create a new button instead of replacing callback_data in case the
                # same object is used elsewhere
                InlineKeyboardButton(
                    btn.text,
                    callback_data=self.__put_button(btn.callback_data, keyboard_data),
                )
                if btn.callback_data
                else btn
                for btn in column
            ]
            for column in reply_markup.inline_keyboard
        ]

        if not keyboard_data.button_data:
            # If we arrive here, no data had to be replaced and we can return the input
            return reply_markup

        self._keyboard_data[keyboard_uuid] = keyboard_data
        return InlineKeyboardMarkup(buttons)

    def process_callback_query(self, callback_query: CallbackQuery) -> CallbackQuery:
        """
        Replaces the data in the callback query and the attached messages keyboard with the cached
        objects, if necessary. If the data could not be found, :class:`InvalidButtonData` will be
        inserted.
        If :attr:`callback_query.data` is present, this also saves the callback queries ID in order
        to be able to resolve it to the stored data.

        Warning:
            *In place*, i.e. the passed :class:`telegram.CallbackQuery` will be changed!

        Args:
            callback_query (:class:`telegram.CallbackQuery`): The callback query.

        Returns:
            The callback query with inserted data.

        """
        with self.__lock:
            if not callback_query.data:
                return callback_query

            # Map the callback queries ID to the keyboards UUID for later use
            self._callback_queries[callback_query.id] = self.extract_uuids(callback_query.data)[0]
            # Get the cached callback data for the CallbackQuery
            callback_query.data = self.__get_button_data(callback_query.data)

            # Get the cached callback data for the inline keyboard attached to the
            # CallbackQuery
            if callback_query.message and callback_query.message.reply_markup:
                for row in callback_query.message.reply_markup.inline_keyboard:
                    for button in row:
                        if button.callback_data:
                            button.callback_data = self.__get_button_data(button.callback_data)

            return callback_query

    def __get_button_data(self, callback_data: str) -> Any:
        keyboard, button = self.extract_uuids(callback_data)
        try:
            # we get the values before calling update() in case KeyErrors are raised
            # we don't want to update in that case
            keyboard_data = self._keyboard_data[keyboard]
            button_data = keyboard_data.button_data[button]
            keyboard_data.update()
            return button_data
        except KeyError:
            return InvalidCallbackData(callback_data)

    def drop_data(self, callback_query: CallbackQuery) -> None:
        """
        Deletes the data for the specified callback query.

        Note:
            Will *not* raise exceptions in case the data is not found in the cache.
            *Will* raise :class:`KeyError` in case the callback query can not be found in the
            cache.

        Args:
            callback_query (:class:`telegram.CallbackQuery`): The callback query.

        Raises:
            KeyError: If the callback query can not be found in the cache
        """
        with self.__lock:
            try:
                keyboard_uuid = self._callback_queries.pop(callback_query.id)
                return self.__drop_keyboard(keyboard_uuid)
            except KeyError as exc:
                raise KeyError('CallbackQuery was not found in cache.') from exc

    def __drop_keyboard(self, keyboard_uuid: str) -> None:
        try:
            self._keyboard_data.pop(keyboard_uuid)
        except KeyError:
            return

    def clear_callback_data(self, time_cutoff: Union[float, datetime] = None) -> None:
        """
        Clears the stored callback data.

        Args:
            time_cutoff (:obj:`float` | :obj:`datetime.datetime`, optional): Pass a UNIX timestamp
                or a :obj:`datetime.datetime` to clear only entries which are older. Naive
                :obj:`datetime.datetime` objects will be assumed to be in UTC.

        """
        with self.__lock:
            self.__clear(self._keyboard_data, time_cutoff)

    def clear_callback_queries(self, time_cutoff: Union[float, datetime] = None) -> None:
        """
        Clears the stored callback query IDs.

        Args:
            time_cutoff (:obj:`float` | :obj:`datetime.datetime`, optional): Pass a UNIX timestamp
                or a :obj:`datetime.datetime` to clear only entries which are older. Naive
                :obj:`datetime.datetime` objects will be assumed to be in UTC.

        """
        with self.__lock:
            self.__clear(self._callback_queries, time_cutoff)

    @staticmethod
    def __clear(mapping: MutableMapping, time_cutoff: Union[float, datetime] = None) -> None:
        if not time_cutoff:
            mapping.clear()
            return

        if isinstance(time_cutoff, datetime):
            effective_cutoff = to_float_timestamp(time_cutoff)
        else:
            effective_cutoff = time_cutoff

        to_drop = (key for key, data in mapping.items() if data.access_time < effective_cutoff)
        for key in to_drop:
            mapping.pop(key)
