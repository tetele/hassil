"""Classes/methods for loading YAML intent files."""

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import IO, Any, Dict, Iterable, List, Optional, Set, Tuple, Union, cast

from yaml import safe_load

from .expression import Expression, Sentence, TextChunk
from .parse_expression import parse_sentence
from .util import is_template, merge_dict, normalize_text


class ResponseType(str, Enum):
    SUCCESS = "success"
    NO_INTENT = "no_intent"
    NO_AREA = "no_area"
    NO_DOMAIN = "no_domain"
    NO_DEVICE_CLASS = "no_device_class"
    NO_ENTITY = "no_entity"
    HANDLE_ERROR = "handle_error"


@dataclass(frozen=True)
class IntentData:
    """Block of sentences and known slots for an intent."""

    sentence_texts: List[str]
    """Sentence templates that match this intent."""

    slots: Dict[str, Any] = field(default_factory=dict)
    """Slot values that are assumed if intent is matched."""

    response: Optional[str] = None
    """Key for response to intent."""

    requires_context: Dict[str, Any] = field(default_factory=dict)
    """Context items required before match is successful."""

    excludes_context: Dict[str, Any] = field(default_factory=dict)
    """Context items that must not be present for match to be successful."""

    expansion_rules: Dict[str, Sentence] = field(default_factory=dict)
    """Local expansion rules in the context of a single intent."""

    wildcard_list_names: Set[str] = field(default_factory=set)

    @cached_property
    def sentences(self) -> List[Sentence]:
        """Sentence templates that match this intent."""
        sentences = [
            parse_sentence(text, keep_text=True) for text in self.sentence_texts
        ]

        # Sort sentences so that wildcards with more literal text chunks are processed first.
        # This will reorder certain wildcards, for example:
        #
        # - "play {album} by {artist}"
        # - "play {album} by {artist} in {room}"
        #
        # will be reordered to:
        #
        # - "play {album} by {artist} in {room}"
        # - "play {album} by {artist}"
        sentences = sorted(sentences, key=self._sentence_order)

        return sentences

    def _sentence_order(self, sentence: Sentence) -> int:
        has_wildcards = False
        if self.wildcard_list_names:
            # Look for wildcard list references
            for list_name in sentence.list_names():
                if list_name in self.wildcard_list_names:
                    has_wildcards = True
                    break

        if has_wildcards:
            # Sentences with more text chunks should be processed sooner
            return -sentence.text_chunk_count()

        return 0


@dataclass
class Intent:
    """A named intent with sentences + slots."""

    name: str
    data: List[IntentData] = field(default_factory=list)


class SlotList(ABC):
    """Base class for slot lists."""


class RangeType(str, Enum):
    """Number range type."""

    NUMBER = "number"
    PERCENTAGE = "percentage"
    TEMPERATURE = "temperature"


@dataclass
class RangeSlotList(SlotList):
    """Slot list for a range of numbers."""

    start: int
    stop: int
    step: int = 1
    type: RangeType = RangeType.NUMBER
    digits: bool = True
    words: bool = True
    words_language: Optional[str] = None
    words_ruleset: Optional[str] = None

    def __post_init__(self):
        """Validate number range"""
        assert self.start < self.stop, "start must be less than stop"
        assert self.step > 0, "step must be positive"
        assert self.digits or self.words, "must have digits, words, or both"


@dataclass
class TextSlotValue:
    """Single value in a text slot list."""

    text_in: Expression
    """Input text for this value"""

    value_out: Any
    """Output value put into slot"""

    context: Optional[Dict[str, Any]] = None
    """Items added to context if value is matched"""

    @staticmethod
    def from_tuple(
        value_tuple: Union[Tuple[str, Any], Tuple[str, Any, Dict[str, Any]]],
        allow_template: bool = True,
    ) -> "TextSlotValue":
        """Construct text slot value from a tuple."""
        text_in, value_out, context = value_tuple[0], value_tuple[1], None

        if len(value_tuple) > 2:
            context = cast(Tuple[str, Any, Dict[str, Any]], value_tuple)[2]

        return TextSlotValue(
            text_in=_maybe_parse_template(text_in, allow_template),
            value_out=value_out,
            context=context,
        )


@dataclass
class TextSlotList(SlotList):
    """Slot list with pre-defined text values."""

    values: List[TextSlotValue]

    @staticmethod
    def from_strings(
        strings: Iterable[str],
        allow_template: bool = True,
    ) -> "TextSlotList":
        """
        Construct a text slot list from strings.

        Input and output values are the same text.
        """
        return TextSlotList(
            values=[
                TextSlotValue(
                    text_in=_maybe_parse_template(text, allow_template), value_out=text
                )
                for text in strings
            ],
        )

    @staticmethod
    def from_tuples(
        tuples: Iterable[Union[Tuple[str, Any], Tuple[str, Any, Dict[str, Any]]]],
        allow_template: bool = True,
    ) -> "TextSlotList":
        """
        Construct a text slot list from text/value pairs.

        Input values are the left (text), output values are the right (any).
        """
        return TextSlotList(
            values=[
                TextSlotValue.from_tuple(value_tuple, allow_template)
                for value_tuple in tuples
            ],
        )


@dataclass
class WildcardSlotList(SlotList):
    """Matches as much text as possible."""

@dataclass
class EntitySlotFilter:
    """Filtering criteria for Home Assistant entities."""

    domain: Optional[str] = None
    device_class: Optional[str] = None

    def apply(self, entity: TextSlotValue) -> bool:
        """Return true if the entity matches filter, false otherwise."""

        if not entity.context:
            return False
        
        if self.domain and entity.context.get("domain", "") != self.domain:
            return False
        
        if self.device_class and entity.context.get("device_class", "") != self.device_class:
            return False
        
        return True


@dataclass
class EntitySlotList(SlotList):
    """Matches entities exposed by Home Assistant."""

    def __init__(self, filters: List[EntitySlotFilter], target_name: str):
        """Creates a new applicable entity list."""

        self.filters = filters
        self.target_name = target_name
    
    def apply(self, slot_lists: Dict[str, Any]) -> "TextSlotList":
        """Applies the current filters on a given slot list."""

        if target := slot_lists.get(self.target_name) is None:
            raise ValueError(f"Missing slot list {{{self.target_name}}}")
        return __class__.apply_filters(self.filters, target)

    @staticmethod
    def apply_filters(filters: List[EntitySlotFilter], target: TextSlotList, allow_template: bool = True) -> "TextSlotList":
        """Apply filters to an existing TextSlotList that we consider to be entities."""

        filtered_entities: Iterable[Union[Tuple[str, Any], Tuple[str, Any, Dict[str, Any]]]] = []

        for entity in target.values:
            skip_value = True
            for filter in filters:
                if filter.apply(entity):
                    skip_value = False
                    break
            
            if skip_value:
                continue
            
            filtered_entities.append(entity)

        return TextSlotList(values=filtered_entities)


@dataclass
class IntentsSettings:
    """Settings for intents."""

    ignore_whitespace: bool = False
    """True if whitespace should be ignored during matching."""


@dataclass
class Intents:
    """Collection of intents, rules, and lists for a language."""

    language: str
    """Language code (e.g., en)."""

    intents: Dict[str, Intent]
    """Intents mapped by name."""

    slot_lists: Dict[str, SlotList] = field(default_factory=dict)
    """Slot lists mapped by name."""

    expansion_rules: Dict[str, Sentence] = field(default_factory=dict)
    """Expansion rules mapped by name."""

    skip_words: List[str] = field(default_factory=list)
    """Words that can be skipped during recognition."""

    settings: IntentsSettings = field(default_factory=IntentsSettings)
    """Settings that may change recognition."""

    @staticmethod
    def from_files(file_paths: Iterable[Union[str, Path]]) -> "Intents":
        """Load intents from YAML file paths."""
        intents_dict: Dict[str, Any] = {}
        for file_path in file_paths:
            with open(file_path, "r", encoding="utf-8") as yaml_file:
                merge_dict(intents_dict, safe_load(yaml_file))

        return Intents.from_dict(intents_dict)

    @staticmethod
    def from_yaml(yaml_file: IO[str]) -> "Intents":
        """Load intents from a YAML file."""
        return Intents.from_dict(safe_load(yaml_file))

    @staticmethod
    def from_dict(input_dict: Dict[str, Any]) -> "Intents":
        """Parse intents from a dict."""
        # language: "<code>"
        # settings:
        #   ignore_whitespace: false
        # intents:
        #   IntentName:
        #     data:
        #       - sentences:
        #           - "<sentence>"
        #         slots:
        #           <slot_name>: <slot value>
        #           <slot_name>:
        #             - <slot value>
        # expansion_rules:
        #   <rule_name>: "<rule body>"
        # lists:
        #   <list_name>:
        #     values:
        #       - "<value>"
        #
        wildcard_list_names: Set[str] = {
            list_name
            for list_name, list_dict in input_dict.get("lists", {}).items()
            if list_dict.get("wildcard", False)
        }
        return Intents(
            language=input_dict["language"],
            intents={
                intent_name: Intent(
                    name=intent_name,
                    data=[
                        IntentData(
                            sentence_texts=data_dict["sentences"],
                            slots=data_dict.get("slots", {}),
                            requires_context=data_dict.get("requires_context", {}),
                            excludes_context=data_dict.get("excludes_context", {}),
                            expansion_rules={
                                rule_name: parse_sentence(rule_body, keep_text=True)
                                for rule_name, rule_body in data_dict.get(
                                    "expansion_rules", {}
                                ).items()
                            },
                            response=data_dict.get("response"),
                            wildcard_list_names=wildcard_list_names,
                        )
                        for data_dict in intent_dict["data"]
                    ],
                )
                for intent_name, intent_dict in input_dict["intents"].items()
            },
            slot_lists={
                list_name: _parse_list(list_dict)
                for list_name, list_dict in input_dict.get("lists", {}).items()
            },
            expansion_rules={
                rule_name: parse_sentence(rule_body, keep_text=True)
                for rule_name, rule_body in input_dict.get(
                    "expansion_rules", {}
                ).items()
            },
            skip_words=input_dict.get("skip_words", []),
            settings=_parse_settings(input_dict.get("settings", {})),
        )


def _parse_list(
    list_dict: Dict[str, Any],
    allow_template: bool = True,
) -> SlotList:
    """Parses a slot list from a dict."""
    if "values" in list_dict:
        # Text values
        text_values: List[TextSlotValue] = []
        for value in list_dict["values"]:
            if isinstance(value, str):
                # String value
                text_values.append(
                    TextSlotValue(
                        text_in=_maybe_parse_template(value, allow_template),
                        value_out=value,
                    )
                )
            else:
                # Object with "in" and "out"
                text_values.append(
                    TextSlotValue(
                        text_in=_maybe_parse_template(value["in"], allow_template),
                        value_out=value["out"],
                        context=value.get("context"),
                    )
                )

        return TextSlotList(text_values)

    if "range" in list_dict:
        # Number range
        range_dict = list_dict["range"]
        return RangeSlotList(
            type=RangeType(range_dict.get("type", "number")),
            start=int(range_dict["from"]),
            stop=int(range_dict["to"]),
            step=int(range_dict.get("step", 1)),
        )

    if list_dict.get("wildcard", False):
        # Wildcard
        return WildcardSlotList()

    if "entities" in list_dict:
        # Filtered entities
        entity_filter_dict = list_dict["entities"]
        filters = entity_filter_dict.get("filters")
        if filters is None:
            filters = []
        if not isinstance(filters, List):
            filters = [filters]
        return EntitySlotList(
            filters=[
                EntitySlotFilter(**filter)
                for filter in filters
            ],
            target_name=entity_filter_dict.get("target")
        )

    raise ValueError(f"Unknown slot list type: {list_dict}")


def _parse_settings(settings_dict: dict[str, Any]) -> IntentsSettings:
    """Parse intent settings."""
    return IntentsSettings(
        ignore_whitespace=settings_dict.get("ignore_whitespace", False)
    )


def _maybe_parse_template(text: str, allow_template: bool = True) -> Expression:
    """Parse string as a sentence template if it has template syntax."""
    if allow_template and is_template(text):
        return parse_sentence(text)

    return TextChunk(normalize_text(text))
