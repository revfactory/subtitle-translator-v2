#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import argparse
import logging
import anthropic
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm


class SubtitleTranslationConfig:
    """자막 번역 관련 설정을 관리하는 클래스"""
    
    DEFAULT_MODEL = "claude-opus-4-5-20251101"
    DEFAULT_BATCH_SIZE = 5
    DEFAULT_MAX_TOKENS = 8000
    DEFAULT_MAX_WORKERS = 3
    DEFAULT_CONFIG_FILE = "config.json"
    
    def __init__(self, config_file: Optional[str] = None):
        # 기본 설정값
        self.model = self.DEFAULT_MODEL
        self.batch_size = self.DEFAULT_BATCH_SIZE
        self.max_tokens = self.DEFAULT_MAX_TOKENS
        self.max_workers = self.DEFAULT_MAX_WORKERS
        self.input_token_cost = 5 / 1_000_000  # 1M 토큰당 $5
        self.output_token_cost = 25 / 1_000_000  # 1M 토큰당 $25
        
        # 명령줄 인자 처리
        self.parser = self._create_argument_parser()
        
        # 기본 설정 파일 로드 시도
        default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.DEFAULT_CONFIG_FILE)
        if os.path.exists(default_config):
            self._load_config_from_file(default_config)
            
        # 사용자 지정 설정 파일 로드 시도 (기본 설정을 덮어씀)
        if config_file and os.path.exists(config_file):
            self._load_config_from_file(config_file)
    
    def _create_argument_parser(self) -> argparse.ArgumentParser:
        """명령줄 인자 파서 생성"""
        parser = argparse.ArgumentParser(description="SRT 자막 번역 도구")
        parser.add_argument("input_file", help="번역할 SRT 파일 경로")
        parser.add_argument("-o", "--output", help="번역된 SRT 파일의 출력 경로")
        parser.add_argument("-m", "--model", help=f"사용할 Claude 모델 (기본값: {self.DEFAULT_MODEL})")
        parser.add_argument("-b", "--batch-size", type=int, help=f"자막 배치 크기 (기본값: {self.DEFAULT_BATCH_SIZE})")
        parser.add_argument("-w", "--workers", type=int, help=f"병렬 작업자 수 (기본값: {self.DEFAULT_MAX_WORKERS})")
        parser.add_argument("-c", "--config", help=f"설정 파일 경로 (기본값: {self.DEFAULT_CONFIG_FILE})")
        parser.add_argument("--gen-config", action="store_true", help="현재 설정으로 기본 설정 파일 생성 후 종료")
        return parser
    
    def _load_config_from_file(self, config_file: str) -> None:
        """설정 파일에서 설정 로드"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                
            # 설정 파일에서 값 가져오기
            self.model = config.get('model', self.model)
            self.batch_size = config.get('batch_size', self.batch_size)
            self.max_tokens = config.get('max_tokens', self.max_tokens)
            self.max_workers = config.get('max_workers', self.max_workers)
            self.input_token_cost = config.get('input_token_cost', self.input_token_cost)
            self.output_token_cost = config.get('output_token_cost', self.output_token_cost)
            
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"설정 파일 로드 중 오류: {e}")
    
    def parse_args(self) -> argparse.Namespace:
        """명령줄 인자 파싱 및 설정 업데이트"""
        args = self.parser.parse_args()
        
        # 명령줄 인자에서 설정 파일 경로 확인
        if args.config:
            self._load_config_from_file(args.config)
        
        # 명령줄 인자로 설정 업데이트
        if args.model:
            self.model = args.model
        if args.batch_size:
            self.batch_size = args.batch_size
        if args.workers:
            self.max_workers = args.workers
        
        return args


class SubtitleFileHandler:
    """자막 파일 입출력 처리 클래스"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def read_srt_file(self, file_path: str) -> str:
        """
        지정된 경로에서 SRT 파일을 읽어 내용을 문자열로 반환
        
        Args:
            file_path: 읽을 SRT 파일 경로
            
        Returns:
            파일 내용 문자열
            
        Raises:
            FileNotFoundError: 파일이 존재하지 않는 경우
            PermissionError: 파일 접근 권한이 없는 경우
            UnicodeDecodeError: 파일 인코딩 문제가 있는 경우
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                return file.read()
        except FileNotFoundError:
            self.logger.error(f"파일을 찾을 수 없습니다: {file_path}")
            raise
        except PermissionError:
            self.logger.error(f"파일 접근 권한이 없습니다: {file_path}")
            raise
        except UnicodeDecodeError:
            self.logger.error(f"파일 인코딩 문제가 발생했습니다. UTF-8이 아닐 수 있습니다: {file_path}")
            raise
        except Exception as e:
            self.logger.error(f"파일 읽기 중 예상치 못한 오류: {e}")
            raise
    
    def write_srt_file(self, file_path: str, content: str) -> None:
        """
        주어진 내용을 SRT 파일로 저장
        
        Args:
            file_path: 저장할 SRT 파일 경로
            content: 저장할 내용
            
        Raises:
            PermissionError: 파일 쓰기 권한이 없는 경우
            IOError: 파일 쓰기 중 오류가 발생한 경우
        """
        try:
            # 디렉토리가 없으면 생성
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write(content)
                
        except PermissionError:
            self.logger.error(f"파일 쓰기 권한이 없습니다: {file_path}")
            raise
        except IOError as e:
            self.logger.error(f"파일 쓰기 중 오류: {e}")
            raise
        except Exception as e:
            self.logger.error(f"파일 쓰기 중 예상치 못한 오류: {e}")
            raise
    
    def validate_srt_format(self, content: str) -> bool:
        """
        SRT 파일 형식이 유효한지 검증
        
        Args:
            content: 검증할 SRT 내용
            
        Returns:
            형식이 유효하면 True, 그렇지 않으면 False
        """
        if not content.strip():
            return False
            
        lines = content.strip().split('\n')
        if len(lines) < 4:  # 최소한 하나의 자막에는 번호, 시간, 내용, 빈 줄이 필요
            return False
            
        return True


class SubtitleProcessor:
    """자막 처리 로직을 담당하는 클래스"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def split_subtitles(self, srt_content: str) -> List[str]:
        """
        SRT 내용을 개별 자막으로 분할
        
        Args:
            srt_content: 원본 SRT 내용
            
        Returns:
            개별 자막 목록
        """
        if not srt_content.strip():
            self.logger.warning("빈 SRT 내용입니다.")
            return []
            
        return srt_content.strip().split('\n\n')
    
    def create_batches(self, subtitles: List[str], batch_size: int) -> List[str]:
        """
        자막 목록을 지정된 크기의 배치로 나눔
        
        Args:
            subtitles: 자막 목록
            batch_size: 배치 크기
            
        Returns:
            배치 목록
        """
        if not subtitles:
            return []
            
        return ['\n\n'.join(subtitles[i:i+batch_size]) for i in range(0, len(subtitles), batch_size)]
    
    def renumber_subtitles(self, srt: str) -> str:
        """
        번역된 자막의 번호를 1부터 순차적으로 다시 매김
        
        Args:
            srt: 재번호 매길 SRT 내용
            
        Returns:
            재번호 매긴 SRT 내용
        """
        if not srt.strip():
            return ""
            
        subtitles = srt.strip().split('\n\n')
        new_subtitles = []
        counter = 1
    
        for subtitle in subtitles:
            lines = subtitle.strip().split('\n')
            if len(lines) >= 2:  # 자막 번호와 시간 정보 등이 있어야 함
                # 첫 번째 줄을 자막 번호로 대체
                lines[0] = str(counter)
                counter += 1
                new_subtitles.append('\n'.join(lines))
    
        return '\n\n'.join(new_subtitles)
        
    def _parse_timestamp(self, timestamp: str) -> float:
        """
        시간 문자열(00:00:00,000)을 초 단위 부동소수점으로 변환
        
        Args:
            timestamp: 시간 문자열 (00:00:00,000 형식)
            
        Returns:
            초 단위 시간 (부동소수점)
        """
        # 시간 형식: 00:00:00,000
        hours, minutes, seconds = timestamp.split(':')
        seconds, milliseconds = seconds.split(',')
        
        total_seconds = (int(hours) * 3600) + (int(minutes) * 60) + int(seconds) + (int(milliseconds) / 1000)
        return total_seconds
        
    def _format_timestamp(self, seconds: float) -> str:
        """
        초 단위 부동소수점을 시간 문자열(00:00:00,000)로 변환
        
        Args:
            seconds: 초 단위 시간 (부동소수점)
            
        Returns:
            시간 문자열 (00:00:00,000 형식)
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        milliseconds = int((seconds - int(seconds)) * 1000)
        
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"
        
    def check_timestamp_overlaps(self, srt: str) -> str:
        """
        자막 시간이 중복되는지 확인하고 중복된 경우 조정
        
        Args:
            srt: 검사할 SRT 내용
            
        Returns:
            시간 중복이 해결된, 조정된 SRT 내용
        """
        if not srt.strip():
            return ""
            
        subtitles = srt.strip().split('\n\n')
        adjusted_subtitles = []
        
        # 이전 자막의 종료 시간을 추적
        prev_end_time = 0
        
        for subtitle in subtitles:
            lines = subtitle.strip().split('\n')
            if len(lines) < 2:  # 최소한 자막 번호와 시간 정보가 필요
                adjusted_subtitles.append(subtitle)
                continue
                
            # 시간 정보 파싱
            try:
                time_line = lines[1]
                start_time_str, end_time_str = time_line.split(' --> ')
                
                start_time = self._parse_timestamp(start_time_str)
                end_time = self._parse_timestamp(end_time_str)
                
                # 시작 시간이 이전 자막 종료 시간보다 빠르면 조정
                if start_time < prev_end_time:
                    self.logger.warning(f"시간 중복 감지: 이전 종료 {self._format_timestamp(prev_end_time)}, 현재 시작 {start_time_str}")
                    # 시작 시간을 이전 자막 종료 시간으로 설정 (50ms 여유)
                    start_time = prev_end_time + 0.05
                    
                    # 종료 시간이 시작 시간보다 빠르면 시작 시간 + 1초로 설정
                    if end_time <= start_time:
                        end_time = start_time + 1.0
                        
                    # 시간 문자열 업데이트
                    start_time_str = self._format_timestamp(start_time)
                    end_time_str = self._format_timestamp(end_time)
                    lines[1] = f"{start_time_str} --> {end_time_str}"
                    
                # 현재 자막의 종료 시간을 다음 자막의 비교를 위해 저장
                prev_end_time = end_time
                
            except (ValueError, IndexError) as e:
                self.logger.warning(f"자막 시간 파싱 중 오류: {e} - 원본 유지: {subtitle}")
                
            # 조정된 자막 추가
            adjusted_subtitles.append('\n'.join(lines))
            
        return '\n\n'.join(adjusted_subtitles)


class ClaudeTranslator:
    """Claude API를 이용한 번역 처리 클래스"""
    
    def __init__(self, config: SubtitleTranslationConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.api_key = self._get_api_key()
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.system_prompt = self._load_system_prompt()
    
    def _get_api_key(self) -> str:
        """
        환경 변수에서 Claude API 키를 가져옴
        
        Returns:
            API 키
            
        Raises:
            ValueError: API 키가 설정되지 않은 경우
        """
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            self.logger.error("환경 변수 'ANTHROPIC_API_KEY'가 설정되지 않았습니다.")
            raise ValueError("환경 변수 'ANTHROPIC_API_KEY'가 설정되지 않았습니다. "
                            "export ANTHROPIC_API_KEY=your_api_key 명령으로 API 키를 설정해주세요.")
        return api_key
    
    def _load_system_prompt(self) -> str:
        """번역용 시스템 프롬프트 로드"""
        return """You are an expert Korean translator specializing in subtitle localization for YouTube videos. Your task is to translate English subtitles into Korean, ensuring high-quality, natural-sounding translations that are easily understood by native Korean speakers.

Follow these instructions to complete the translation:

1. Read through the entire set of English subtitles to understand the context and flow of the video content.

2. Translate each subtitle from English to Korean, adhering to these guidelines:
   a. Ensure each Korean subtitle is no longer than 30 characters. If a translation exceeds this limit, split it into two or more subtitles and adjust the timing accordingly.
   b. Use appropriate honorifics and formality levels based on the context of the video.
   c. Adapt any culturally specific references or idioms to Korean equivalents that convey the same meaning.
   d. For terms that are better left in English (e.g., brand names, technical terms), use the original English term followed by a brief Korean explanation in parentheses if necessary.
   e. Use line breaks to improve readability, considering the natural flow of the Korean language.

3. Consider the context of surrounding subtitles to ensure your translations are coherent and natural-sounding when viewed in sequence.

4. Format your translation in SRT structure, maintaining the original numbering and timing where possible. When splitting subtitles, use incremental numbering (e.g., 3, 3a, 3b) and adjust timestamps accordingly.

5. Review your translation for accuracy, naturalness, and proper formatting before submitting your final output.

For complex or challenging subtitles, conduct your analysis within <subtitle_analysis> tags using the following process:

1. Original English: [Insert original English subtitle]
2. Key phrases/concepts: [Break down the subtitle into main ideas or phrases]
3. Initial translations:
   - Phrase 1: [Provide multiple Korean translation options]
   - Phrase 2: [Provide multiple Korean translation options]
   ...
4. Word choice reasoning: [Explain why you chose specific Korean words or expressions]
5. Video context consideration: [Describe how the video context affects your translation choices]
6. Character count: [Count characters in the proposed translation]
7. Adjustments (if needed):
   - Split subtitle: [Show how you'd split the subtitle if over 30 characters]
   - Revised timing: [Provide adjusted timestamps for split subtitles]
8. Cultural adaptations: [Explain any cultural references you adapted]
9. Final translation: [Provide the final Korean translation(s) with timing]

Use this process for any subtitles that require special attention or are particularly challenging to translate.

Your final output should be in the following format:

<korean_subtitles>
1
[start time] --> [end time]
[Korean translation (≤30 characters, 1 sentense)]

2
[start time] --> [end time]
[Korean translation (≤30 characters, 1 sentense)]

...
</korean_subtitles>

Remember to prioritize natural, easily understandable Korean translations while adhering to the 30-character limit and considering the context of the entire video."""
    
    def translate_batch(self, batch: str, start_number: int) -> Tuple[str, int, int]:
        """
        주어진 배치의 자막을 번역
        
        Args:
            batch: 번역할 자막 배치
            start_number: 시작 자막 번호
            
        Returns:
            (번역된 자막, 입력 토큰 수, 출력 토큰 수)
            
        Raises:
            Exception: 번역 중 오류가 발생한 경우
        """
        if not batch.strip():
            return "", 0, 0
            
        try:
            message = self.client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
                messages=[
                    {"role": "user", "content": batch}
                ]
            )
    
            # 토큰 사용량 추출
            usage = message.usage
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
    
            translated_text = message.content[0].text
            
            # <korean_subtitles> 태그 사이의 내용 추출
            try:
                korean_subtitles = translated_text.split('<korean_subtitles>')[1].split('</korean_subtitles>')[0].strip()
                return korean_subtitles + '\n\n', input_tokens, output_tokens
            except IndexError:
                self.logger.warning("번역된 텍스트에서 <korean_subtitles> 태그를 찾을 수 없습니다. 전체 응답을 반환합니다.")
                return translated_text + '\n\n', input_tokens, output_tokens
                
        except anthropic.APIError as e:
            self.logger.error(f"Claude API 오류: {e}")
            raise
        except Exception as e:
            self.logger.error(f"번역 중 오류 발생: {e}")
            raise


class SubtitleTranslator:
    """전체 자막 번역 프로세스를 관리하는 클래스"""
    
    def __init__(self, config: SubtitleTranslationConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.file_handler = SubtitleFileHandler()
        self.processor = SubtitleProcessor()
        self.translator = ClaudeTranslator(config)
        
        # 토큰 사용량 추적 변수
        self.total_input_tokens = 0
        self.total_output_tokens = 0
    
    def _translate_batch_with_retry(self, batch: str, start_number: int) -> Tuple[str, int, int]:
        """
        재시도 로직을 포함한 배치 번역
        
        Args:
            batch: 번역할 자막 배치
            start_number: 시작 자막 번호
            
        Returns:
            (번역된 자막, 입력 토큰 수, 출력 토큰 수)
        """
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                return self.translator.translate_batch(batch, start_number)
            except Exception as e:
                retry_count += 1
                self.logger.warning(f"배치 번역 시도 {retry_count}/{max_retries} 실패: {e}")
                
                if retry_count >= max_retries:
                    self.logger.error("최대 재시도 횟수를 초과했습니다.")
                    return f"[번역 실패: {e}]\n\n", 0, 0
                    
                # 지수 백오프 적용
                wait_time = 2 ** retry_count
                self.logger.info(f"{wait_time}초 후 재시도합니다...")
                time.sleep(wait_time)
    
    def _translate_batch_task(self, args: Tuple[str, int, int]) -> Tuple[int, str, int, int]:
        """
        병렬 처리를 위한 번역 작업 함수
        
        Args:
            args: (배치, 시작 번호, 배치 인덱스)
            
        Returns:
            (배치 인덱스, 번역된 자막, 입력 토큰 수, 출력 토큰 수)
        """
        batch, start_number, batch_index = args
        translated_batch, input_tokens, output_tokens = self._translate_batch_with_retry(batch, start_number)
        return batch_index, translated_batch, input_tokens, output_tokens
    
    def translate(self, input_file: str, output_file: str) -> Dict:
        """
        전체 자막 번역 실행
        
        Args:
            input_file: 번역할 SRT 파일 경로
            output_file: 번역 결과를 저장할 파일 경로
            
        Returns:
            번역 결과 통계 (토큰 수, 비용 등)
        """
        self.logger.info(f"파일 '{input_file}'을(를) 번역합니다...")
        
        try:
            # 입력 파일 읽기 및 검증
            srt_content = self.file_handler.read_srt_file(input_file)
            
            if not self.file_handler.validate_srt_format(srt_content):
                self.logger.error("유효하지 않은 SRT 파일 형식입니다.")
                raise ValueError("유효하지 않은 SRT 파일 형식입니다.")
            
            # 자막 분할 및 배치 생성
            subtitles = self.processor.split_subtitles(srt_content)
            self.logger.info(f"총 {len(subtitles)}개의 자막을 찾았습니다.")
            
            batches = self.processor.create_batches(subtitles, self.config.batch_size)
            self.logger.info(f"자막을 {len(batches)}개의 배치로 나누었습니다.")
            
            # 번역 작업 준비
            results = [None] * len(batches)
            
            # 병렬 처리 실행
            batch_tasks = [(batch, i * self.config.batch_size + 1, i) for i, batch in enumerate(batches)]
            
            with ThreadPoolExecutor(max_workers=min(self.config.max_workers, len(batches))) as executor:
                futures = [executor.submit(self._translate_batch_task, task) for task in batch_tasks]
                
                # tqdm으로 진행 상황 표시
                with tqdm(total=len(batches), desc="번역 진행 중") as progress_bar:
                    for future in futures:
                        batch_index, translated_batch, input_tokens, output_tokens = future.result()
                        results[batch_index] = translated_batch
                        
                        # 토큰 사용량 누적
                        self.total_input_tokens += input_tokens
                        self.total_output_tokens += output_tokens
                        
                        progress_bar.update(1)
            
            # 번역 결과 결합
            translated_srt = "".join(results)
            
            # 자막 번호 재정렬
            translated_srt = self.processor.renumber_subtitles(translated_srt)
            
            # 시간 중복 확인 및 조정
            self.logger.info("자막 시간 중복 확인 및 조정 중...")
            original_translated_srt = translated_srt
            adjusted_translated_srt = self.processor.check_timestamp_overlaps(translated_srt)
            
            # 수정된 내용이 있는지 확인
            if original_translated_srt != adjusted_translated_srt:
                self.logger.info("시간 중복이 감지되어 자동으로 조정되었습니다.")
                translated_srt = adjusted_translated_srt
            else:
                self.logger.info("시간 중복이 발견되지 않았습니다.")
            
            # 결과 저장
            self.file_handler.write_srt_file(output_file, translated_srt)
            self.logger.info(f"번역 완료! 결과가 {output_file}에 저장되었습니다.")
            
            # 비용 계산
            total_cost = (self.total_input_tokens * self.config.input_token_cost) + (self.total_output_tokens * self.config.output_token_cost)
            
            stats = {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "total_cost": total_cost,
                "subtitles_count": len(subtitles),
                "batches_count": len(batches)
            }
            
            self.logger.info(f"총 사용된 입력 토큰: {self.total_input_tokens}")
            self.logger.info(f"총 사용된 출력 토큰: {self.total_output_tokens}")
            self.logger.info(f"총 요금: ${total_cost:.4f}")
            
            return stats
            
        except Exception as e:
            self.logger.error(f"번역 중 오류가 발생했습니다: {e}")
            raise


def setup_logging():
    """로깅 설정"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )


def generate_default_config(config: SubtitleTranslationConfig):
    """현재 설정으로 기본 설정 파일 생성"""
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.DEFAULT_CONFIG_FILE)
    config_data = {
        "model": config.model,
        "batch_size": config.batch_size,
        "max_tokens": config.max_tokens,
        "max_workers": config.max_workers,
        "input_token_cost": config.input_token_cost,
        "output_token_cost": config.output_token_cost
    }
    
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2)
        print(f"기본 설정 파일이 생성되었습니다: {config_file}")
        return True
    except Exception as e:
        print(f"설정 파일 생성 중 오류: {e}")
        return False

def main():
    """메인 함수"""
    # 로깅 설정
    setup_logging()
    logger = logging.getLogger(__name__)
    
    try:
        # 설정 초기화 및 명령줄 인자 파싱
        config = SubtitleTranslationConfig()
        args = config.parse_args()
        
        # 설정 파일 생성 요청 확인
        if args.gen_config:
            success = generate_default_config(config)
            sys.exit(0 if success else 1)
        
        # 입출력 파일 경로 설정
        input_file = args.input_file
        
        if args.output:
            output_file = args.output
        else:
            # 출력 파일 이름 자동 생성
            base, ext = os.path.splitext(os.path.basename(input_file))
            output_file_name = f"{base}_ko{ext}"
            output_dir = os.path.dirname(os.path.abspath(input_file))
            output_file = os.path.join(output_dir, output_file_name)
        
        # 번역기 초기화 및 실행
        translator = SubtitleTranslator(config)
        stats = translator.translate(input_file, output_file)
        
        # 결과 요약 출력
        logger.info("번역 완료 요약:")
        logger.info(f"- 처리된 자막 수: {stats['subtitles_count']}")
        logger.info(f"- 배치 수: {stats['batches_count']}")
        logger.info(f"- 입력 토큰: {stats['input_tokens']}")
        logger.info(f"- 출력 토큰: {stats['output_tokens']}")
        logger.info(f"- 총 비용: ${stats['total_cost']:.4f}")
        
    except KeyboardInterrupt:
        logger.info("사용자에 의해 프로그램이 중단되었습니다.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류가 발생했습니다: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
