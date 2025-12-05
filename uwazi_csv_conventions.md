# Uwazi CSV Conventions

Entities in an Uwazi collection are the fundamental types of information. An Entity is a collection of various data elements, referred to as Properties, and can hold associated files and relationships.

## Entity Data Structures

| Data Type / Component         | Description                                              | Structure / Format for CSV Import                                                                 |
|------------------------------|----------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| Entity                       | The core information record (e.g., case, person, event). | Defined by a Template which dictates its structure (Properties).                                  |
| Name / Title                 | The primary identifying name of the Entity.              | Must be in the column labeled Title.                                                              |
| Properties                   | Specific data fields on an Entity, defined by its Template.| Columns must be labeled with the property title.                                                  |
| Primary Document             | The main associated PDF file for an Entity.              | Column labeled File containing the document's file name (e.g., Example Doc1.pdf). Requires ZIP file import. |
| Supporting Files (Attachments)| Other associated files (images, docs, etc.).            | Column labeled Attachments containing file names separated by a pipe symbol (`\|`).                |
| Text & Rich Text             | Standard text information.                               | Direct text entry in the corresponding column.                                                    |
| Date                         | A specific calendar date.                                | Must follow the collection's default date format (e.g., YYYY/MM/DD).                              |
| Multiple Date                | Multiple specific calendar dates.                        | Dates separated by a pipe symbol (`\|`).                                                           |
| Date Range                   | A span of time with a start and end date.                | Start and end dates separated by a colon symbol (:) without spaces (e.g., 2000/01/01:2000/12/01). Missing dates use a blank space (e.g., :2000/12/01). |
| Multiple Date Range          | Multiple spans of time.                                  | Date ranges separated by a pipe symbol (`\|`).                                                     |
| Select                       | Single selection from a Thesaurus list.                  | The corresponding thesauri value (term) is used.                                                  |
| Select (Grouped Thesaurus)   | Single selection from a two-level (grouped) Thesaurus.   | Group followed by double colon (::) then the term (e.g., Asia::Bangladesh).                      |
| Multiple Select              | Multiple selections from a Thesaurus list.               | Terms separated by a pipe symbol (`\|`).                                                           |
| Relationship                 | Links to one or more other Entities.                     | Title of the target Entity/Entities. Multiple titles are separated by a pipe symbol (`\|`).        |
| Link                         | A URL link.                                              | The URL must contain the protocol (http or https). For a labeled link, use label followed by a pipe symbol (`\|`). |
| Geolocation                  | Latitude and longitude coordinates.                      | Column header must be labeled: [property-name]_geolocation. Coordinates in decimal degrees (DD) are formatted as latitude followed by a pipe symbol (`\|`). |
| Generated ID                 | An automatically generated identifier.                   | The Title column should be left blank in the CSV file.                                            |
| Multilingual Properties      | Text fields with content in multiple languages.          | Additional columns are created for each language, labeled as [property-name]__language-code (e.g., Address__fr). |

## Key Data Separators

The CSV import guidelines rely on specific separators for handling multiple values or complex data structures within a single cell:

- **Pipe Symbol (`|`)**: Used to separate multiple values for:
    - Multiple Select properties
    - Multiple Relationships
    - Multiple Date properties
    - Multiple Date Range properties
    - Multiple Supporting Files/Attachments
    - Geolocation coordinates (separates latitude and longitude)
    - Link property (separates the label and the URL)
- **Colon Symbol (`:`)**: Used to separate the start and end dates for a Date Range property (e.g., start:end).
- **Double Colon Symbol (`::`)**: Used to separate the Group and the Term for grouped Select properties (e.g., Group::Term).
