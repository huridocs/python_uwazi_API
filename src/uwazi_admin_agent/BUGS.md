## List bugs

* In the @src/uwazi_admin_agent review ui the button for synch with uwazi when it is drifted does not seem a button, set border or something so it seems a button
* In the review table the top panel with the filters buttons messages of drift and buttons to select and collapse rows looks not tidy or not cohesive. Can you improve the look and arregement of the above elements to the table
* In the wizard when the AI came up with questions, there is one that should not be asked as it is part of the system flow, not the user to decide. More precisely, the llm script should produce suggestions based on the input, but it should not use the actual values in Uwazi never, and the AI is asking something related to the current values in the entity. Furthermore, it should not ask if the new suggestions should be appended or replaced, because again, the vaslues in the entities should not be taken into consideration for the script. Take a look to the questiuonn from the AI:
  
Which set should be suggested for `states` on an entity whose recommendation text yields matches?
Only the states extracted from the recommendation text
The extracted states plus any states already linked to the entity
Only the extracted states that are not yet linked to the entity
Your answer

* 

## Done

* Validation rows in the review table had no suggestions and read "excluded" instead of "validate": the evaluation pass scoped every read to the held-out ids (so a classify-by-relationship script could not resolve any name and recorded `skipped` for all of them), the frozen label was looked up under the row's language while a language-independent property has none, and a `skipped` holdout row fell into the display-only "excluded" branch. The pass now runs with the production read surface and records the frozen label on every held-out entity it has a label for.
