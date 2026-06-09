from typing import Optional

from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.llm_port import LlmPort
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
    "properties. Before updating entities in bulk, look up the template to know each "
    "property's type and which thesaurus values are valid. Identify every entity by its "
    "``shared_id`` — never by title (titles are not unique and can change). To find ids, "
    "search first with ``search_entities_by_text``; to inspect details, fetch by id with "
    "``get_entities_by_shared_ids``. ``update_entities`` performs a partial merge: only "
    "the fields you provide are changed. Pass thesaurus values as labels, never as UUIDs."
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
    ):
        self.llm = llm
        self.thesauri_api = thesauri_api
        self.template_api = template_api
        self.template_mapper = template_mapper
        self.thesauri_mapper = thesauri_mapper
        self.entity_api = entity_api

    async def execute(self, task_description: str, context: str = "") -> str:
        prompt = self._compose_prompt(task_description=task_description, context=context)
        deps = UwaziAgentToolsDependencies(
            thesauri_api=self.thesauri_api,
            template_api=self.template_api,
            template_mapper=self.template_mapper,
            thesauri_mapper=self.thesauri_mapper,
            entity_api=self.entity_api,
        )
        agent = build_uwazi_agent(
            model=self.llm.get_model(),
            deps_type=UwaziAgentToolsDependencies,
            instructions=DEFAULT_INSTRUCTIONS,
            include_entities=self.entity_api is not None,
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
