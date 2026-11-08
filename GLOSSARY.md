# Document Recognition

Domain language for the OCR service being designed. This glossary describes the target scope, not changes already implemented.

## Language

**OCR service**:
A service that returns recognized text from supplied documents without classifying document types or extracting predefined business fields.
_Avoid_: Expense extraction service (when referring to OCR-only processing)

**Recognized text**:
Text read from a supplied document, returned both as combined document text and as text associated with individual pages. It is distinct from structured business fields extracted from that text.
_Avoid_: Structured extraction (when referring only to recognized text)

**Consumer team**:
A team within the organization that uses the shared OCR service. Multiple consumer teams share the service.
