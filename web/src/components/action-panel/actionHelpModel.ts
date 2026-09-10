import type { ActionTemplate } from '../../api/types';

export interface ActionHelpItem {
  label: string;
  detail?: string;
}

export interface ActionHelpContent {
  summary: string;
  how: string;
  inputs: ActionHelpItem[];
  output: string;
  cost: string;
  goodFor: string;
}

/**
 * The concise, human-authored part of action help. Keeping this exhaustive
 * makes a newly registered built-in fail typechecking until somebody explains
 * it; catalog/plugin actions still receive useful metadata-derived help.
 */
export const BUILTIN_ACTION_HELP: Partial<Record<string, { how: string; goodFor: string }>> = {
  'map.classify': {
    how: 'Compares each selected row with the labels and instructions you provide, then chooses the best fitting label.',
    goodFor: 'Sorting mixed records into a small, known set of topics, stances, priorities, or review buckets.',
  },
  'map.extract': {
    how: 'Reads each row independently and returns the structured fields you define, leaving unsupported values blank.',
    goodFor: 'Turning narrative documents into names, dates, amounts, identifiers, and other exportable fields.',
  },
  'map.mcp_extract': {
    how: 'Reads each row independently, can call every current tool from the local MCP servers you select, then validates the structured fields you define.',
    goodFor: 'Enriching or resolving rows with trusted local tools such as internal databases, taxonomies, files, and specialized services.',
  },
  'map.find': {
    how: 'Scans every addressable part of the selected sources, then creates one grounded result row for each matching occurrence.',
    goodFor: 'Finding discussions, quotes, passages, or visible image regions and reviewing each match in source context.',
  },
  'map.summarize': {
    how: 'Condenses the selected source text for each row according to your requested length and focus.',
    goodFor: 'Creating skimmable briefs from articles, transcripts, filings, or other long text.',
  },
  'map.ask': {
    how: 'Answers the same question against every row using only that row’s selected source material.',
    goodFor: 'Checking many records for the same fact, claim, omission, or follow-up question.',
  },
  'enrich.geocode': {
    how: 'Sends each address or place name to the selected geocoder and stores the returned coordinates.',
    goodFor: 'Preparing addresses for a map or comparing records by location.',
  },
  'map.translate': {
    how: 'Translates each selected text value between the chosen languages with the selected local or hosted engine.',
    goodFor: 'Making multilingual records searchable and reviewable while preserving the original column.',
  },
  'research.web_search': {
    how: 'Builds a search query for each row and saves the provider’s result titles, snippets, and links.',
    goodFor: 'Finding public context or leads for a list of people, organizations, places, or claims.',
  },
  'media.transcribe': {
    how: 'Runs speech recognition over each audio or video cell and preserves timestamps when the engine provides them.',
    goodFor: 'Making interviews, meetings, hearings, and video searchable and ready for text analysis.',
  },
  'temporal.extract_range': {
    how: 'Cuts the chosen time range from one audio or video source without changing the original media.',
    goodFor: 'Saving a quote, exchange, or scene as a focused clip.',
  },
  'map.find_visual_cuts': {
    how: 'Detects substantial visual changes in each video and records editable scene-boundary timestamps.',
    goodFor: 'Navigating long videos or preparing scene-based clips and review.',
  },
  'map.find_topic_sections': {
    how: 'Analyzes a timestamped transcript and groups consecutive speech into editable topical ranges.',
    goodFor: 'Finding agenda changes and discussion sections in long recordings.',
  },
  'derive.temporal_segments': {
    how: 'Uses timestamps or selected ranges to create a child sheet with one playable media segment per row.',
    goodFor: 'Turning a long recording into clips that can be reviewed or analyzed independently.',
  },
  'derive.transcript_segments': {
    how: 'Uses transcript timestamps or topic ranges to create a child sheet of timed transcript sections.',
    goodFor: 'Reviewing, classifying, or summarizing separate parts of a long conversation.',
  },
  'media.ocr': {
    how: 'Reads visible characters from images or scanned PDF pages with the selected recognition engine.',
    goodFor: 'Making scans, photographs, and image-only documents searchable.',
  },
  'media.extract_metadata': {
    how: 'Reads technical metadata embedded in supported media and file cells without interpreting their content.',
    goodFor: 'Collecting dimensions, durations, formats, dates, and other file properties.',
  },
  'media.to_markdown': {
    how: 'Converts the contents and layout of supported documents into readable Markdown text.',
    goodFor: 'Preparing PDFs and office documents for search, review, summarization, and extraction.',
  },
  'web.capture_page': {
    how: 'Visits each public URL and either saves a durable page capture or collects its links into a child sheet.',
    goodFor: 'Preserving web evidence or building a crawl list from index and document pages.',
  },
  'web.capture_screenshot': {
    how: 'Opens each public URL in a real browser and saves an image of the full page or current viewport.',
    goodFor: 'Keeping a visual record of a web page’s rendered appearance for review or a gallery.',
  },
  'derive.table_from_list': {
    how: 'Creates a new child sheet from selected columns and rows while preserving lineage to the source.',
    goodFor: 'Making a focused working set without altering the original sheet.',
  },
  'media.extract_pdf_tables': {
    how: 'Detects tables in PDF files and creates structured rows and columns from their cells.',
    goodFor: 'Recovering tabular data from reports, filings, and published records.',
  },
  'join.semantic': {
    how: 'Embeds source and target text, then proposes the closest target match for each source row with a score.',
    goodFor: 'Matching names or descriptions that refer to the same thing but are not spelled identically.',
  },
  'derive.join': {
    how: 'Matches equal key values across two sheets and creates a materialized sheet containing columns from both.',
    goodFor: 'Combining records that share an ID, case number, filename, or other exact key.',
  },
  'map.python': {
    how: 'Runs your Python expression or function independently for each selected row in the controlled action runtime.',
    goodFor: 'Custom calculations and transformations that are awkward to express with built-in actions.',
  },
  'map.api_call': {
    how: 'Builds and sends one HTTP request per selected row, using column values in the URL, headers, or body.',
    goodFor: 'Enriching rows from a trusted API or sending records through an external service.',
  },
  'map.columns_from_json': {
    how: 'Reads object-shaped JSON values and expands selected properties into ordinary sheet columns.',
    goodFor: 'Flattening API responses or other nested results so they can be filtered and exported.',
  },
  'research.answer': {
    how: 'Lets the configured research agent plan and use its available tools to answer a broader task for each row.',
    goodFor: 'Multi-step research questions that need more than one lookup or transformation.',
  },
  'map.regex_extract': {
    how: 'Applies a regular-expression pattern to the chosen text and returns the requested match or capture group.',
    goodFor: 'Reliably extracting formatted IDs, dates, amounts, emails, and other predictable text patterns.',
  },
  'cluster.values': {
    how: 'Groups similar values, lets you review the proposed groups, and writes the chosen canonical value.',
    goodFor: 'Reconciling spelling, capitalization, and naming variations before joins or analysis.',
  },
  'resolve.substitute': {
    how: 'Replaces complete cell values according to the mapping you review, with an explicit rule for unmatched values.',
    goodFor: 'Normalizing known aliases, codes, categories, and recurring variants.',
  },
  'resolve.replace': {
    how: 'Runs ordered contains, exact, or regex rules across every row and replaces each matching cell’s entire value with the rule target.',
    goodFor: 'Reclassifying or normalizing whole-cell values when exact mapping is not enough and rule order matters.',
  },
  'resolve.combine': {
    how: 'Maps several reviewed source values into broader groups and writes the selected group name.',
    goodFor: 'Rolling detailed categories up into a smaller reporting taxonomy.',
  },
  'resolve.fill_missing': {
    how: 'Creates a new column, preserves present values, and fills missing cells from neighboring rows, a constant, or a column aggregate.',
    goodFor: 'Completing sparse columns with fill down, fill up, a fixed value, mean, median, or mode without changing the source column.',
  },
  'map.template': {
    how: 'Renders a text template once per row, substituting values from the columns you reference.',
    goodFor: 'Building labels, URLs, search queries, briefs, and other consistent text from existing fields.',
  },
  'reduce.group_summary': {
    how: 'Combines values from many rows into a smaller set of aggregate results according to your instructions.',
    goodFor: 'Producing an overview, rollup, or synthesis across a collection rather than row by row.',
  },
  'map.judge': {
    how: 'Evaluates an existing AI-generated column against the rubric you provide and records a review result.',
    goodFor: 'Spotting weak classifications, summaries, or extractions that need human attention.',
  },
  'enrich.census_demographics': {
    how: 'Looks up selected Census variables for each geography and saves the returned values as columns.',
    goodFor: 'Adding population and demographic context to places or geographic identifiers.',
  },
  'map.clean_column': {
    how: 'Applies predictable text cleanup such as trimming whitespace and normalizing case to each value.',
    goodFor: 'Preparing inconsistent text for filtering, grouping, deduplication, or joins.',
  },
  'map.clean_dates': {
    how: 'Parses recognizable date values and writes them in one consistent format.',
    goodFor: 'Sorting and comparing dates imported in mixed formats.',
  },
  'media.ytdlp_download': {
    how: 'Downloads media from each public URL and stores it as a local project file cell.',
    goodFor: 'Keeping durable, playable copies of externally hosted audio, video, or images.',
  },
  'export.column_tables': {
    how: 'Collects table-valued cells from a column and exports CSV files and a manifest in a ZIP.',
    goodFor: 'Taking extracted document tables into a spreadsheet for further analysis.',
  },
  'media.extract_faces': {
    how: 'Detects faces in each image and writes cropped face regions with their source locations.',
    goodFor: 'Reviewing who appears across a collection of photographs or video frames.',
  },
  'media.fetch_url': {
    how: 'Requests each public URL and stores the response content and request outcome for that row.',
    goodFor: 'Importing public pages or machine-readable endpoints referenced by a sheet.',
  },
  'map.ner': {
    how: 'Detects named people, organizations, places, dates, and other entity types in the selected text.',
    goodFor: 'Scanning long text for recurring actors and creating highlighted document annotations.',
  },
  'map.to_geo_point': {
    how: 'Combines latitude and longitude values into a typed geographic point column.',
    goodFor: 'Turning existing coordinates into data Frisket can display on a map.',
  },
  'media.video_frames': {
    how: 'Samples still images from each video at the requested interval or timestamps.',
    goodFor: 'Creating a visual index or preparing frames for OCR and image analysis.',
  },
};

export function buildActionHelp(action: ActionTemplate): ActionHelpContent {
  const authored = BUILTIN_ACTION_HELP[action.kind];
  const sourceInputs = (action.sourceRequirements ?? []).map((requirement) => ({
    label: requirement.label,
    detail: requirement.message,
  }));
  const requiredParams = (action.params ?? [])
    .filter((param) => param.required)
    .map((param) => ({
      label: param.label,
      detail: param.hint ?? param.placeholder,
    }));
  const inputs = [...sourceInputs, ...requiredParams];
  if (inputs.length === 0) {
    inputs.push({
      label: 'Current sheet',
      detail: 'Choose the source and options shown in the form, then preview before running.',
    });
  }

  const namedOutputs = action.defaultFields
    .map((field) => field.name)
    .filter(Boolean);
  const destination = action.produces === 'sheet'
    ? 'Creates a new sheet.'
    : 'Writes results back to the current sheet.';
  const output = namedOutputs.length > 0
    ? `${destination} Default output: ${namedOutputs.join(', ')}.`
    : `${destination} The exact output shape and destination are shown in the form.`;

  let cost: string;
  if (action.llm) {
    cost = 'Uses the selected model. Preview shows the estimated cost before a paid run.';
  } else if (action.externalMetered) {
    cost = 'Calls an external service or public source. Frisket shows any required confirmation before running.';
  } else if ((action.engines?.length ?? 0) > 0) {
    cost = 'Cost and local requirements depend on the selected engine; the form shows availability and estimates.';
  } else {
    cost = 'Runs without a language-model charge.';
  }

  const keywordCopy = (action.keywords ?? []).slice(0, 4).join(', ');
  const goodFor = keywordCopy
    ? `${action.description} Common searches: ${keywordCopy}.`
    : action.description;

  return {
    summary: action.actionDescription ?? action.description,
    how: authored?.how ?? 'Uses the settings in this form to process the selected rows without changing the original source values.',
    inputs,
    output,
    cost,
    goodFor: authored?.goodFor ?? goodFor,
  };
}
