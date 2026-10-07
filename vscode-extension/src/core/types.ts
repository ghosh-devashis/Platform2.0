/** Shapes emitted by `ent-agent check --json` (all positions 1-based). */
export interface FixEdit {
  line: number;
  column: number;
  end_line: number;
  end_column: number;
  new_text: string;
}

export interface Fix {
  description: string;
  edits: FixEdit[];
  imports?: string[];
}

export interface Finding {
  rule: string;
  severity: 'error' | 'warning';
  message: string;
  path: string;
  line: number;
  column: number;
  end_line: number;
  end_column: number;
  fix?: Fix;
}

/** 0-based text edit (VS Code convention). */
export interface TextEdit0 {
  line: number;
  character: number;
  endLine: number;
  endCharacter: number;
  newText: string;
}
