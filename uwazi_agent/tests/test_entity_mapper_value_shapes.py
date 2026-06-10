"""Unit tests for ``EntityMapper`` value-shape coercion.

These tests focus on the read and write paths for property values that
are easy for the LLM to get wrong: geolocation, link, date range. They
pin down the LLM-facing shape on the way out and the set of accepted
shapes on the way in, including the defensive envelope unwraps added to
make the agent more forgiving of LLM round-trip mistakes.
"""

from unittest.mock import MagicMock

import pytest

from uwazi_agent.adapters.uwazi_api.entity_mapper import EntityMapper
from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.thesauri import Thesauri, ThesauriValue


def _make_mapper() -> EntityMapper:
    template_repo = MagicMock()
    template_repo.get_by_name.return_value = None
    template_repo.get_by_id.return_value = None
    thesauri_repo = MagicMock()
    thesauri_repo.get.return_value = []
    return EntityMapper(template_repo=template_repo, thesauri_repo=thesauri_repo)


def _make_mapper_with_template(template: Template) -> EntityMapper:
    template_repo = MagicMock()
    template_repo.get_by_name.return_value = template
    template_repo.get_by_id.return_value = template
    thesauri_repo = MagicMock()
    thesauri_repo.get.return_value = []
    return EntityMapper(template_repo=template_repo, thesauri_repo=thesauri_repo)


def _build_template_with(props: list[PropertySchema]) -> Template:
    return Template(id="tpl1", name="T", properties=props)


def _build_entity(metadata: dict, prop_name: str, prop_type: PropertyType) -> Entity:
    """Build a single-property API entity whose on-disk envelope is ``{value: ...}``."""
    return Entity(
        sharedId="sid",
        title="X",
        template="tpl1",
        language="en",
        metadata=metadata,
    )


class TestGeolocationRead:
    def setup_method(self):
        self.template = _build_template_with(
            [PropertySchema(name="location", label="Location", type=PropertyType.GEO_LOCATION)]
        )
        self.mapper = _make_mapper_with_template(self.template)

    def test_read_returns_lat_lon_pair(self):
        """On read, the LLM-facing shape is always ``[lat, lon]`` (no label)."""
        entity = _build_entity(
            {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": "Valencia, Spain"}}]},
            "location",
            PropertyType.GEO_LOCATION,
        )
        agent = self.mapper.to_agent(entity, template_name="T")
        assert agent.metadata["location"] == [39.4699, -0.3763]

    def test_read_unwraps_inner_dict(self):
        """Defensive: handles the read shape where the inner dict has no outer ``value``."""
        entity = _build_entity(
            {"location": [{"lat": 1.0, "lon": 2.0}]},
            "location",
            PropertyType.GEO_LOCATION,
        )
        agent = self.mapper.to_agent(entity, template_name="T")
        assert agent.metadata["location"] == [1.0, 2.0]


class TestGeolocationWrite:
    def setup_method(self):
        self.template = _build_template_with(
            [PropertySchema(name="location", label="Location", type=PropertyType.GEO_LOCATION)]
        )
        self.mapper = _make_mapper_with_template(self.template)

    def _build_agent(self, value):
        from uwazi_agent.domain.agent_entity import AgentEntity

        return AgentEntity(
            shared_id="sid",
            title="X",
            template_name="T",
            metadata={"location": value},
        )

    def test_accepts_list_pair(self):
        agent = self._build_agent([39.4699, -0.3763])
        api = self.mapper.to_api(agent)
        assert api.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": ""}}]}

    def test_accepts_dict(self):
        agent = self._build_agent({"lat": 39.4699, "lon": -0.3763})
        api = self.mapper.to_api(agent)
        assert api.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": ""}}]}

    def test_accepts_pipe_string(self):
        agent = self._build_agent("39.4699|-0.3763")
        api = self.mapper.to_api(agent)
        assert api.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": ""}}]}

    def test_accepts_read_shape_round_trip(self):
        """The LLM can copy the read shape ``[[lat, lon]]`` back verbatim."""
        agent = self._build_agent([[39.4699, -0.3763]])
        api = self.mapper.to_api(agent)
        assert api.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": ""}}]}

    def test_accepts_envelope_dict_round_trip(self):
        """Defensive: LLM echoing back the on-disk envelope ``[{lat, lon, label}]``."""
        agent = self._build_agent([{"lat": 39.4699, "lon": -0.3763, "label": "Valencia, Spain"}])
        api = self.mapper.to_api(agent)
        assert api.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": "Valencia, Spain"}}]}

    def test_accepts_envelope_wrapped_in_value(self):
        agent = self._build_agent({"value": {"lat": 39.4699, "lon": -0.3763, "label": "X"}})
        api = self.mapper.to_api(agent)
        assert api.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": "X"}}]}

    def test_rejects_bare_pipe_string(self):
        agent = self._build_agent("foo|bar")
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "Geolocation value" in str(exc.value)
        assert "[lat, lon]" in str(exc.value)

    def test_rejects_unknown_shape(self):
        agent = self._build_agent(42)
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "Geolocation value" in str(exc.value)

    def test_rejects_dict_without_lat_lon(self):
        agent = self._build_agent({"label": "X"})
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "lat" in str(exc.value).lower() and "lon" in str(exc.value).lower()


class TestLinkRead:
    def setup_method(self):
        self.template = _build_template_with([PropertySchema(name="source", label="Source", type=PropertyType.LINK)])
        self.mapper = _make_mapper_with_template(self.template)

    def test_read_unwraps_envelope(self):
        entity = _build_entity(
            {"source": [{"value": {"label": "Uwazi", "url": "https://uwazi.io"}}]},
            "source",
            PropertyType.LINK,
        )
        agent = self.mapper.to_agent(entity, template_name="T")
        assert agent.metadata["source"] == {"label": "Uwazi", "url": "https://uwazi.io"}


class TestLinkWrite:
    def setup_method(self):
        self.template = _build_template_with([PropertySchema(name="source", label="Source", type=PropertyType.LINK)])
        self.mapper = _make_mapper_with_template(self.template)

    def _build_agent(self, value):
        from uwazi_agent.domain.agent_entity import AgentEntity

        return AgentEntity(
            shared_id="sid",
            title="X",
            template_name="T",
            metadata={"source": value},
        )

    def test_accepts_dict(self):
        agent = self._build_agent({"label": "Uwazi", "url": "https://uwazi.io"})
        api = self.mapper.to_api(agent)
        assert api.metadata == {"source": [{"value": {"label": "Uwazi", "url": "https://uwazi.io"}}]}

    def test_accepts_pipe_string(self):
        agent = self._build_agent("Uwazi|https://uwazi.io")
        api = self.mapper.to_api(agent)
        assert api.metadata == {"source": [{"value": {"label": "Uwazi", "url": "https://uwazi.io"}}]}

    def test_accepts_envelope_dict(self):
        agent = self._build_agent({"value": {"label": "Uwazi", "url": "https://uwazi.io"}})
        api = self.mapper.to_api(agent)
        assert api.metadata == {"source": [{"value": {"label": "Uwazi", "url": "https://uwazi.io"}}]}

    def test_rejects_unknown_shape(self):
        agent = self._build_agent(42)
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "Link value" in str(exc.value)


class TestDateRangeRead:
    def setup_method(self):
        self.template = _build_template_with([PropertySchema(name="period", label="Period", type=PropertyType.DATE_RANGE)])
        self.mapper = _make_mapper_with_template(self.template)

    def test_read_unwraps_envelope(self):
        entity = _build_entity(
            {"period": [{"value": {"from": 1704067200, "to": 1735603200}}]},
            "period",
            PropertyType.DATE_RANGE,
        )
        agent = self.mapper.to_agent(entity, template_name="T")
        # ``from``/``to`` come through as the inner epoch numbers; the mapper only
        # unwraps, not re-encodes, on the read path.
        assert agent.metadata["period"] == {"from": 1704067200, "to": 1735603200}


class TestDateRangeWrite:
    def setup_method(self):
        self.template = _build_template_with([PropertySchema(name="period", label="Period", type=PropertyType.DATE_RANGE)])
        self.mapper = _make_mapper_with_template(self.template)

    def _build_agent(self, value):
        from uwazi_agent.domain.agent_entity import AgentEntity

        return AgentEntity(
            shared_id="sid",
            title="X",
            template_name="T",
            metadata={"period": value},
        )

    def test_accepts_dict(self):
        agent = self._build_agent({"from": "2024-01-01", "to": "2024-12-31"})
        api = self.mapper.to_api(agent)
        assert "period" in api.metadata

    def test_accepts_shorthand(self):
        agent = self._build_agent("2024-01-01->2024-12-31")
        api = self.mapper.to_api(agent)
        assert "period" in api.metadata

    def test_accepts_envelope_dict(self):
        agent = self._build_agent({"value": {"from": "2024-01-01", "to": "2024-12-31"}})
        api = self.mapper.to_api(agent)
        assert "period" in api.metadata

    def test_rejects_unknown_shape(self):
        agent = self._build_agent(42)
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "Date range" in str(exc.value)

    def test_rejects_empty_range(self):
        agent = self._build_agent({})
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "from" in str(exc.value) and "to" in str(exc.value)


class TestSelectRead:
    """``select`` and ``multiselect`` go through a thesaurus lookup on read."""

    def setup_method(self):
        self.template = _build_template_with(
            [PropertySchema(name="status", label="Status", type=PropertyType.SELECT, content="th1")]
        )
        self.mapper = _make_mapper_with_template(self.template)
        self.thesauri_repo = MagicMock()
        self.thesauri_repo.get.return_value = [
            Thesauri(
                _id="th1",
                name="Status",
                values=[
                    ThesauriValue(id="v1", label="Approved"),
                    ThesauriValue(id="v2", label="Pending"),
                ],
            )
        ]
        self.mapper._thesauri_repo = self.thesauri_repo

    def test_read_resolves_uuid_to_label(self):
        entity = _build_entity(
            {"status": [{"value": "v1"}]},
            "status",
            PropertyType.SELECT,
        )
        agent = self.mapper.to_agent(entity, template_name="T")
        assert agent.metadata["status"] == "Approved"


class TestSelectWrite:
    def setup_method(self):
        self.template = _build_template_with(
            [PropertySchema(name="status", label="Status", type=PropertyType.SELECT, content="th1")]
        )
        self.mapper = _make_mapper_with_template(self.template)
        self.thesauri_repo = MagicMock()
        self.thesauri_repo.get.return_value = [
            Thesauri(
                _id="th1",
                name="Status",
                values=[
                    ThesauriValue(id="v1", label="Approved"),
                    ThesauriValue(id="v2", label="Pending"),
                ],
            )
        ]
        self.mapper._thesauri_repo = self.thesauri_repo

    def test_accepts_label(self):
        from uwazi_agent.domain.agent_entity import AgentEntity

        agent = AgentEntity(
            shared_id="sid",
            title="X",
            template_name="T",
            metadata={"status": "Approved"},
        )
        api = self.mapper.to_api(agent)
        # Note: SELECT returns the thesaurus id directly, not wrapped in
        # the ``{"value": ...}`` envelope. This is the pre-existing
        # behaviour of ``_coerce_value`` for select/multiselect; tested
        # here only to confirm labels are resolved to ids on the way out.
        assert api.metadata == {"status": ["v1"]}

    def test_rejects_unknown_label(self):
        from uwazi_agent.domain.agent_entity import AgentEntity

        agent = AgentEntity(
            shared_id="sid",
            title="X",
            template_name="T",
            metadata={"status": "NotAValidLabel"},
        )
        with pytest.raises(SearchError) as exc:
            self.mapper.to_api(agent)
        assert "NotAValidLabel" in str(exc.value)


class TestRoundTrip:
    """A read followed by a write with no changes must produce an equivalent envelope."""

    def test_geolocation_round_trip(self):
        template = _build_template_with([PropertySchema(name="location", label="Location", type=PropertyType.GEO_LOCATION)])
        mapper = _make_mapper_with_template(template)
        # Read
        api_entity = _build_entity(
            {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": "Valencia, Spain"}}]},
            "location",
            PropertyType.GEO_LOCATION,
        )
        agent_entity = mapper.to_agent(api_entity, template_name="T")
        # The LLM uses the read shape verbatim when round-tripping
        assert agent_entity.metadata["location"] == [39.4699, -0.3763]
        # Write
        api_entity2 = mapper.to_api(agent_entity)
        assert api_entity2.metadata == {"location": [{"value": {"lat": 39.4699, "lon": -0.3763, "label": ""}}]}

    def test_link_round_trip(self):
        template = _build_template_with([PropertySchema(name="source", label="Source", type=PropertyType.LINK)])
        mapper = _make_mapper_with_template(template)
        api_entity = _build_entity(
            {"source": [{"value": {"label": "Uwazi", "url": "https://uwazi.io"}}]},
            "source",
            PropertyType.LINK,
        )
        agent_entity = mapper.to_agent(api_entity, template_name="T")
        assert agent_entity.metadata["source"] == {"label": "Uwazi", "url": "https://uwazi.io"}
        api_entity2 = mapper.to_api(agent_entity)
        assert api_entity2.metadata == {"source": [{"value": {"label": "Uwazi", "url": "https://uwazi.io"}}]}
