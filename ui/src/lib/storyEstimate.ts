/**
 * Page → word → chapter arithmetic for the Storywriter length control.
 *
 * Mirrors WORDS_PER_PAGE / auto_chapter_count / chapter_target_words in
 * app/services/story_pipeline.py. Duplicated on purpose: the slider has to
 * update on every drag, and a round trip per pixel would be absurd. The
 * backend stays authoritative — /api/v1/story/estimate returns the same
 * numbers, and only the backend's values reach the model.
 */

export const WORDS_PER_PAGE = 275

/** Target chapter length the pipeline aims for when picking a count. */
const TARGET_CHAPTER_WORDS = 1500
const MIN_CHAPTERS = 3
const MAX_CHAPTERS = 200

export interface StoryEstimate {
  totalWords: number
  chapters: number
  wordsPerChapter: number
}

export function estimateStory(minPages: number, chapterCount: number | null): StoryEstimate {
  const pages = Math.max(1, Math.floor(minPages) || 1)
  const totalWords = pages * WORDS_PER_PAGE
  const chapters = chapterCount && chapterCount > 0
    ? Math.floor(chapterCount)
    : Math.min(MAX_CHAPTERS, Math.max(MIN_CHAPTERS, Math.round(totalWords / TARGET_CHAPTER_WORDS)))
  return {
    totalWords,
    chapters,
    wordsPerChapter: Math.max(1, Math.round(totalWords / chapters)),
  }
}
