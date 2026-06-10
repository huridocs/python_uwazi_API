from typing import Optional

from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_agent.ports.thesauri_mapper_port import ThesauriMapperPort
from uwazi_agent.use_cases.agent_factory import build_uwazi_agent
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


DEFAULT_INSTRUCTIONS = (
    "You are an assistant that manages a Uwazi instance. Use the provided tools to "
    "look up, create, update, or delete thesauri and templates by their human-readable "
    "name. Always identify a thesaurus or template by its name, never by an internal id. "
    "Confirm the result of any mutation back to the user in plain language.\n\n"
    "Templates describe the shape of an entity. To create one, give it a unique name "
    "and the list of custom ``properties`` (each with a ``name`` and ``type``). The "
    "platform-managed common properties (title, creationDate, editDate) are added "
    "automatically — never include them. For properties of type ``select`` or "
    "``multiselect``, set ``thesaurus_name`` to the name of the thesaurus to link to; "
    "the mapper will resolve it to the Uwazi id. Property types of ``relationship`` are "
    "not supported yet (TODO).\n\n"
    "Entities belong to a template; their ``metadata`` shape is defined by the template's "
    "properties. Before creating or updating entities in bulk, look up the template to "
    "know each property's type and which thesaurus values are valid. To create brand-new "
    "entities use ``create_entities`` — provide a ``title`` and ``template_name`` and never "
    "a ``shared_id`` (Uwazi mints it and returns it to you). To change existing entities "
    "use ``update_entities``, which performs a partial merge: only the fields you provide "
    "are changed. Identify existing entities by their ``shared_id`` — never by title (titles "
    "are not unique and can change). To find ids, search first with ``search_entities_by_text``; "
    "to inspect details, fetch by id with ``get_entities_by_shared_ids``. Pass thesaurus "
    "values as labels, never as UUIDs.\n\n"
    "Pages are standalone Settings → Pages (landing pages, about pages, documentation, "
    "dashboards). A page's ``content`` is its body, rendered as Markdown, and raw HTML is "
    "allowed inside it — use Markdown headings, lists, tables and HTML blocks (e.g. "
    '``<div align="center">``, ``<img>``) to make pages genuinely beautiful. The optional '
    "``javascript`` field is custom code that runs on the public page; leave it empty unless "
    "asked. Create pages with ``create_pages`` (never pass a ``shared_id`` — Uwazi mints it "
    "and returns it together with the public ``url``). Identify existing pages by their "
    "``shared_id``, never by title: discover ids with ``list_pages`` and read full bodies "
    "with ``get_pages_by_shared_ids`` before editing. ``update_pages`` is a partial merge — "
    "only the fields you set change; fetch the current ``content`` first if you want to tweak "
    "rather than replace it. Always confirm before ``delete_pages_by_shared_ids`` since "
    "deletions are irreversible."
)


class RunAgentUseCase:
    def __init__(
        self,
        llm: LlmPort,
        thesauri_api: ThesauriApiPort,
        template_api: TemplateApiPort,
        template_mapper: TemplateMapperPort,
        thesauri_mapper: Optional[ThesauriMapperPort] = None,
        entity_api: Optional[EntityApiPort] = None,
        page_api: Optional[PageApiPort] = None,
    ):
        self.llm = llm
        self.thesauri_api = thesauri_api
        self.template_api = template_api
        self.template_mapper = template_mapper
        self.thesauri_mapper = thesauri_mapper
        self.entity_api = entity_api
        self.page_api = page_api

    async def execute(self, task_description: str, context: str = "") -> str:
        prompt = self._compose_prompt(task_description=task_description, context=context)
        deps = UwaziAgentToolsDependencies(
            thesauri_api=self.thesauri_api,
            template_api=self.template_api,
            template_mapper=self.template_mapper,
            thesauri_mapper=self.thesauri_mapper,
            entity_api=self.entity_api,
            page_api=self.page_api,
        )
        agent = build_uwazi_agent(
            model=self.llm.get_model(),
            deps_type=UwaziAgentToolsDependencies,
            instructions=DEFAULT_INSTRUCTIONS,
            include_entities=self.entity_api is not None,
            include_pages=self.page_api is not None,
        )
        result = await agent.run(prompt, deps=deps)
        return result.output

    @staticmethod
    def _compose_prompt(task_description: str, context: str) -> str:
        context = (context or "").strip()
        task = (task_description or "").strip()
        if context:
            return f"Context:\n{context}\n\nTask:\n{task}"
        return task
