#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import threading
import subprocess
import re
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                            QTabWidget, QLabel, QPushButton, QLineEdit, QFileDialog, 
                            QTextEdit, QProgressBar, QComboBox, QSpinBox, QMessageBox,
                            QGroupBox, QRadioButton, QCheckBox, QScrollArea)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QIcon, QDesktopServices, QFont, QColor

# 상위 디렉토리 추가하여 기존 모듈 import 가능하게 함
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 기존 모듈 import
from subtitle import SubtitleTranslator, SubtitleTranslationConfig

class RedirectOutput:
    """출력을 GUI로 리다이렉트"""
    def __init__(self, text_widget):
        self.text_widget = text_widget
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        if '\n' in self.buffer:
            lines = self.buffer.split('\n')
            self.buffer = lines[-1]
            for line in lines[:-1]:
                self.text_widget.append(line)
        self.text_widget.ensureCursorVisible()

    def flush(self):
        if self.buffer:
            self.text_widget.append(self.buffer)
            self.buffer = ""


class TranslationThread(QThread):
    """자막 번역을 위한 스레드"""
    update_progress = pyqtSignal(int, int)
    update_status = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, input_file, output_file, config):
        super().__init__()
        self.input_file = input_file
        self.output_file = output_file
        self.config = config
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr

    def run(self):
        try:
            self.update_status.emit(f"파일 '{self.input_file}'을(를) 번역합니다...")
            translator = SubtitleTranslator(self.config)
            
            # 원본 translate 메소드를 저장
            original_translate = translator.translate
            
            # batches 변수를 추적하기 위한 래퍼 함수
            def translate_wrapper(input_file, output_file):
                # 입력 파일 읽기 및 검증
                srt_content = translator.file_handler.read_srt_file(input_file)
                
                if not translator.file_handler.validate_srt_format(srt_content):
                    translator.logger.error("유효하지 않은 SRT 파일 형식입니다.")
                    raise ValueError("유효하지 않은 SRT 파일 형식입니다.")
                
                # 자막 분할 및 배치 생성
                subtitles = translator.processor.split_subtitles(srt_content)
                translator.logger.info(f"총 {len(subtitles)}개의 자막을 찾았습니다.")
                
                batches = translator.processor.create_batches(subtitles, translator.config.batch_size)
                translator.logger.info(f"자막을 {len(batches)}개의 배치로 나누었습니다.")
                
                # 진행 상황 업데이트를 위한 콜백 함수
                original_translate_batch_task = translator._translate_batch_task
                
                def progress_hook(args):
                    batch_index, translated_batch, input_tokens, output_tokens = original_translate_batch_task(args)
                    self.update_progress.emit(batch_index + 1, len(batches))
                    return batch_index, translated_batch, input_tokens, output_tokens
                
                # 원본 함수 대체
                translator._translate_batch_task = progress_hook
                
                # 원본 translate 함수 호출
                return original_translate(input_file, output_file)
            
            # translate 메소드를 래퍼로 대체
            translator.translate = translate_wrapper
            
            stats = translator.translate(self.input_file, self.output_file)
            self.update_status.emit(f"번역 완료! 결과가 {self.output_file}에 저장되었습니다.")
            
            # 결과 요약 생성
            summary = f"""
            번역 완료 요약:
            - 처리된 자막 수: {stats['subtitles_count']}
            - 배치 수: {stats['batches_count']}
            - 입력 토큰: {stats['input_tokens']}
            - 출력 토큰: {stats['output_tokens']}
            - 총 비용: ${stats['total_cost']:.4f}
            """
            
            self.finished_signal.emit(True, summary)
            
        except Exception as e:
            self.update_status.emit(f"오류 발생: {str(e)}")
            self.finished_signal.emit(False, str(e))


class YoutubeDownloadThread(QThread):
    """YouTube 동영상 다운로드 스레드"""
    update_status = pyqtSignal(str)
    update_progress = pyqtSignal(float)  # 진행 상황을 위한 시그널 추가 (0~100 범위)
    finished_signal = pyqtSignal(bool, str, str)

    def __init__(self, youtube_url, download_directory=None, extract_audio=False):
        super().__init__()
        self.youtube_url = youtube_url
        self.download_directory = download_directory or os.path.expanduser("~/Downloads")
        self.extract_audio = extract_audio

    def run(self):
        try:
            # 다운로드 종류에 따른 메시지 설정
            if self.extract_audio:
                self.update_status.emit(f"YouTube 오디오 다운로드 중 (MP3): {self.youtube_url}")
            else:
                self.update_status.emit(f"YouTube 동영상 다운로드 중: {self.youtube_url}")
            
            # 작업 디렉토리를 다운로드 디렉토리로 변경
            current_dir = os.getcwd()
            os.chdir(self.download_directory)
            
            # yt-dlp 명령 설정
            if self.extract_audio:
                # MP3 추출 명령 (오디오만 다운로드)
                cmd = [
                    "yt-dlp", 
                    "-x",  # 오디오 추출 옵션
                    "--audio-format", "mp3",  # MP3 형식으로 변환
                    "--audio-quality", "0",  # 최고 품질 (0은 최고, 9는 최저)
                    "--newline",
                    self.youtube_url
                ]
            else:
                # 일반 비디오 다운로드 명령
                cmd = [
                    "yt-dlp", 
                    "-f", "bestvideo+bestaudio/best", 
                    "--newline", 
                    self.youtube_url
                ]
            
            # yt-dlp 실시간 출력 처리를 위한 Popen 사용
            process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            filename = None
            
            # 출력을 실시간으로 처리
            for line in iter(process.stdout.readline, ''):
                # 로그 출력
                self.update_status.emit(line.strip())
                
                # 진행률 추출 (다운로드 중)
                if '[download]' in line and '%' in line:
                    try:
                        # "[download]  50.0% of ~20.00MiB at  5.00MiB/s ETA 00:10" 같은 형식에서 백분율 추출
                        percent_str = line.split('[download]')[1].strip().split('%')[0].strip()
                        percent = float(percent_str)
                        self.update_progress.emit(percent)
                    except (ValueError, IndexError):
                        pass
                
                # 비디오 파일명 추출
                if "Merging formats into" in line:
                    match = re.search(r'Merging formats into "(.*?)"', line)
                    if match:
                        filename = match.group(1)
                
                # MP3 파일명 추출 (오디오 추출 시)
                if "Destination:" in line and self.extract_audio:
                    match = re.search(r'Destination:\s+(.+\.mp3)', line)
                    if match:
                        filename = match.group(1)
                
                # MP3 변환 완료 확인
                if "[ExtractAudio] Destination:" in line and self.extract_audio:
                    match = re.search(r'Destination:\s+(.+\.mp3)', line)
                    if match:
                        filename = match.group(1)
            
            # 프로세스 완료 대기
            return_code = process.wait()
            
            if return_code != 0:
                self.update_status.emit("다운로드 실패")
                self.finished_signal.emit(False, "", "다운로드 실패")
                return
            
            if not filename:
                # 파일명을 찾지 못한 경우, 디렉토리에서 가장 최근에 수정된 파일을 찾음
                files = os.listdir(self.download_directory)
                files = [f for f in files if os.path.isfile(os.path.join(self.download_directory, f))]
                
                if self.extract_audio:
                    # MP3 파일만 필터링
                    files = [f for f in files if f.lower().endswith('.mp3')]
                else:
                    # 비디오 파일만 필터링 (일반적인 비디오 확장자)
                    files = [f for f in files if f.lower().endswith(('.mp4', '.mkv', '.webm'))]
                
                if files:
                    # 가장 최근에 수정된 파일 선택
                    files.sort(key=lambda x: os.path.getmtime(os.path.join(self.download_directory, x)), reverse=True)
                    filename = files[0]
                    self.update_status.emit(f"최근 파일을 찾았습니다: {filename}")
                else:
                    self.update_status.emit("다운로드된 파일명을 찾을 수 없습니다.")
                    self.finished_signal.emit(False, "", "파일명을 찾을 수 없음")
                    return
            
            # [videoID] 부분 제거
            clean_filename = re.sub(r'\s*\[[a-zA-Z0-9_-]+\](\.[^.]+)$', r'\1', filename)
            
            # 파일 이름 변경
            if clean_filename != filename:
                try:
                    os.rename(filename, clean_filename)
                    self.update_status.emit(f"파일명 변경: {filename} -> {clean_filename}")
                    filename = clean_filename
                except Exception as e:
                    self.update_status.emit(f"파일명 변경 실패: {e}")
            
            # 절대 경로 생성 (작업 디렉토리를 복원하기 전에)
            full_path = os.path.abspath(filename)
            
            # 100% 진행률 표시
            self.update_progress.emit(100.0)
            
            # 다운로드 종류에 따른 메시지
            if self.extract_audio:
                self.update_status.emit(f"MP3 오디오 다운로드 완료: {filename}")
                self.finished_signal.emit(True, full_path, "MP3 다운로드 완료")
            else:
                self.update_status.emit(f"동영상 다운로드 완료: {filename}")
                self.finished_signal.emit(True, full_path, "다운로드 완료")
            
        except Exception as e:
            self.update_status.emit(f"다운로드 중 오류 발생: {str(e)}")
            self.finished_signal.emit(False, "", str(e))
        finally:
            # 작업 디렉토리 복원
            os.chdir(current_dir)


class ExtractSubtitleThread(QThread):
    """자막 추출 스레드"""
    update_status = pyqtSignal(str)
    update_progress = pyqtSignal(float)  # 진행 상황을 위한 시그널 추가
    finished_signal = pyqtSignal(bool, str, str)

    def __init__(self, video_filename):
        super().__init__()
        self.video_filename = video_filename

    def run(self):
        try:
            if not self.video_filename or not os.path.exists(self.video_filename):
                self.update_status.emit(f"파일을 찾을 수 없습니다: {self.video_filename}")
                self.finished_signal.emit(False, "", "파일을 찾을 수 없음")
                return
            
            self.update_status.emit(f"자막 추출 시작: {os.path.basename(self.video_filename)}")
            
            # AssemblyAI 명령어를 실시간 출력 처리로 실행
            process = subprocess.Popen(
                ["assemblyai", "transcribe", self.video_filename, "--srt"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            srt_filename = None
            
            # 실시간 출력 처리
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if line:
                    # 상태 업데이트
                    self.update_status.emit(line)
                    
                    # 진행률 추출 시도
                    progress_updated = False
                    
                    # AssemblyAI의 "Uploading file to our servers" 형식 파싱
                    if "Uploading file to our servers:" in line:
                        try:
                            # "Uploading file to our servers:  18.35 MB / 40.68 MB   45.11%" 형식 파싱
                            import re as regex
                            percent_match = regex.search(r'(\d+(?:\.\d+)?)%\s*$', line)
                            if percent_match:
                                percent = float(percent_match.group(1))
                                # 업로드는 전체 진행률의 30%로 간주
                                adjusted_percent = percent * 0.3
                                self.update_progress.emit(adjusted_percent)
                                progress_updated = True
                        except (ValueError, AttributeError):
                            pass
                    
                    # AssemblyAI의 다른 진행 상황 메시지들
                    elif "Processing audio..." in line or "Transcribing audio..." in line:
                        # 처리 시작 시 30%로 설정
                        self.update_progress.emit(30.0)
                        progress_updated = True
                    elif "Audio processing complete" in line or "Transcription complete" in line:
                        # 처리 완료 시 85%로 설정
                        self.update_progress.emit(85.0)
                        progress_updated = True
                    
                    # 일반적인 퍼센트 표시 파싱
                    if not progress_updated and "%" in line:
                        try:
                            import re as regex
                            percent_match = regex.search(r'(\d+(?:\.\d+)?)%', line)
                            if percent_match:
                                percent = float(percent_match.group(1))
                                
                                # 업로드 단계 (0-30%)
                                if "upload" in line.lower():
                                    adjusted_percent = percent * 0.3
                                # 처리/전사 단계 (30-90%)
                                elif any(keyword in line.lower() for keyword in ["transcrib", "process", "analyz"]):
                                    adjusted_percent = 30 + (percent * 0.6)
                                # 다운로드/저장 단계 (90-100%)
                                elif any(keyword in line.lower() for keyword in ["download", "saving", "creat"]):
                                    adjusted_percent = 90 + (percent * 0.1)
                                else:
                                    adjusted_percent = percent
                                
                                self.update_progress.emit(min(adjusted_percent, 99.0))
                                progress_updated = True
                        except (ValueError, AttributeError):
                            pass
                    
                    # 키워드 기반 단계별 진행률 (퍼센트가 없는 경우)
                    if not progress_updated:
                        if "uploading" in line.lower() or "upload" in line.lower():
                            self.update_progress.emit(5.0)
                        elif "transcribing" in line.lower() or "processing" in line.lower() or "analyzing" in line.lower():
                            self.update_progress.emit(40.0)
                        elif "downloading" in line.lower() or "saving" in line.lower() or "creating" in line.lower():
                            self.update_progress.emit(85.0)
                    
                    # SRT 파일명 추출
                    if "Successfully created file" in line:
                        match = re.search(r'Successfully created file ([a-zA-Z0-9-]+\.srt)', line)
                        if match:
                            srt_filename = match.group(1)
            
            # 프로세스 완료 대기
            return_code = process.wait()
            
            if return_code != 0:
                self.update_status.emit("자막 추출 실패")
                self.finished_signal.emit(False, "", "자막 추출 실패")
                return
            
            if not srt_filename:
                self.update_status.emit("생성된 SRT 파일명을 찾을 수 없습니다.")
                self.finished_signal.emit(False, "", "SRT 파일명을 찾을 수 없음")
                return
            
            # 동영상 파일명과 동일하게 SRT 파일명 변경
            base_filename = os.path.splitext(self.video_filename)[0]
            new_srt_filename = f"{base_filename}.srt"
            
            try:
                os.rename(srt_filename, new_srt_filename)
                self.update_status.emit(f"SRT 파일명 변경: {srt_filename} -> {new_srt_filename}")
            except Exception as e:
                self.update_status.emit(f"SRT 파일명 변경 실패: {e}")
                self.finished_signal.emit(False, "", f"SRT 파일명 변경 실패: {e}")
                return
            
            # 절대 경로로 변환
            full_srt_path = os.path.abspath(new_srt_filename)
            
            # 100% 진행률 표시
            self.update_progress.emit(100.0)
            self.update_status.emit(f"자막 추출 완료: {new_srt_filename}")
            self.finished_signal.emit(True, full_srt_path, "자막 추출 완료")
            
        except Exception as e:
            self.update_status.emit(f"자막 추출 중 오류 발생: {str(e)}")
            self.finished_signal.emit(False, "", str(e))


class SubtitleTranslatorApp(QMainWindow):
    """자막 번역 앱 메인 윈도우"""
    
    def __init__(self):
        super().__init__()
        
        # 초기화 순서 변경: 속성부터 먼저 초기화
        self.translator_thread = None
        self.youtube_thread = None
        self.extract_thread = None
        self.extract_only_thread = None  # 단독 자막 추출용 스레드 추가
        self.downloaded_video = None
        self.extracted_subtitle = None
        
        # 기본 저장 위치 설정
        self.download_directory = os.path.expanduser("~/Downloads")
        
        # UI 초기화 및 설정 로드
        self.init_ui()
        self.config = self.load_config()
        
        # 프로그레스 바 초기화
        self.progress_bar.setValue(0)
        self.yt_progress_bar.setValue(0)
        self.extract_progress_bar.setValue(0)
        
        # 설정 파일에서 저장 위치 로드
        self.load_directories_from_config()
        # UI에 저장 위치 반영
        if hasattr(self, 'download_dir_edit'):
            self.download_dir_edit.setText(self.download_directory)
        
    def init_ui(self):
        """UI 초기화"""
        self.setWindowTitle("자막 번역 도구")
        self.setGeometry(100, 100, 900, 700)
        self.setMinimumSize(800, 600)
        
        # 모던 스타일 설정
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f5f5f7;
            }
            QWidget {
                font-family: 'Pretendard', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
                color: #333333;
                font-size: 13px;
            }
            QTabWidget::pane { 
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                background-color: white;
                padding: 5px;
            }
            QTabBar::tab {
                background-color: #f0f0f0;
                color: #666666;
                border: 1px solid #e0e0e0;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                min-width: 8ex;
                padding: 8px 16px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #4a86e8;
                color: white;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background-color: #e6e6e6;
            }
            QPushButton {
                background-color: #4a86e8;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3a76d8;
            }
            QPushButton:pressed {
                background-color: #2a66c8;
            }
            QLineEdit, QTextEdit, QComboBox, QSpinBox {
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                padding: 8px;
                background-color: white;
                min-height: 28px;
            }
            QLineEdit:focus, QTextEdit:focus {
                border: 1px solid #4a86e8;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 16px;
                background-color: rgba(255, 255, 255, 0.7);
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 5px;
                color: #4a86e8;
            }
            QProgressBar {
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                background-color: white;
                text-align: center;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #4a86e8;
                border-radius: 5px;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
            }
            QCheckBox::indicator:checked {
                background-color: #4a86e8;
                border: 1px solid #4a86e8;
            }
            QComboBox {
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                padding: 6px 12px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: right center;
                width: 20px;
                border-left: none;
            }
            QSpinBox {
                padding-right: 15px;
            }
            QTextEdit {
                background-color: #f8f8f8;
                color: #444;
                border-radius: 6px;
                font-family: 'Menlo', 'Consolas', monospace;
                font-size: 13px;
            }
        """)
        
        # 메인 위젯과 레이아웃
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 15, 20, 20)
        main_layout.setSpacing(12)
        
        # 탭 위젯 생성
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        main_layout.addWidget(self.tabs)

        # 탭 1: YouTube 다운로드
        self.tab_youtube = QWidget()
        self.tabs.addTab(self.tab_youtube, "YouTube 다운로드")
        self.setup_youtube_tab()

        # 탭 2: 자막 추출
        self.tab_extract = QWidget()
        self.tabs.addTab(self.tab_extract, "자막 추출")
        self.setup_extract_tab()

        # 탭 3: 자막 번역
        self.tab_translate = QWidget()
        self.tabs.addTab(self.tab_translate, "자막 번역")
        self.setup_translate_tab()
        
        # 탭 4: 설정
        self.tab_settings = QWidget()
        self.tabs.addTab(self.tab_settings, "설정")
        self.setup_settings_tab()
        
        # 하단 상태 바
        self.statusBar().showMessage("준비됨")
        self.statusBar().setStyleSheet("""
            QStatusBar {
                background-color: #4a86e8;
                color: white;
                padding: 5px;
                font-weight: bold;
            }
        """)
        
        # 앱 아이콘 설정
        # self.setWindowIcon(QIcon("app/icon.png"))
        
    def setup_translate_tab(self):
        """자막 번역 탭 설정"""
        layout = QVBoxLayout(self.tab_translate)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # 파일 선택 그룹
        file_group = QGroupBox("입력 파일")
        file_layout = QHBoxLayout()
        file_layout.setContentsMargins(12, 20, 12, 12)
        file_layout.setSpacing(8)
        
        self.input_file_edit = QLineEdit()
        self.input_file_edit.setPlaceholderText("번역할 SRT 파일 선택")
        self.input_file_edit.setReadOnly(True)
        
        browse_button = QPushButton("찾아보기")
        browse_button.setIcon(QIcon.fromTheme("document-open"))
        browse_button.clicked.connect(self.browse_input_file)
        
        file_layout.addWidget(self.input_file_edit, 4)
        file_layout.addWidget(browse_button, 1)
        file_group.setLayout(file_layout)
        layout.addWidget(file_group)
        
        # 출력 파일 그룹
        output_group = QGroupBox("출력 파일")
        output_layout = QHBoxLayout()
        output_layout.setContentsMargins(12, 20, 12, 12)
        output_layout.setSpacing(8)
        
        self.output_file_edit = QLineEdit()
        self.output_file_edit.setPlaceholderText("출력 SRT 파일 경로 (자동 생성)")
        self.output_file_edit.setReadOnly(True)
        
        output_browse_button = QPushButton("찾아보기")
        output_browse_button.setIcon(QIcon.fromTheme("document-save"))
        output_browse_button.clicked.connect(self.browse_output_file)
        
        output_layout.addWidget(self.output_file_edit, 4)
        output_layout.addWidget(output_browse_button, 1)
        output_group.setLayout(output_layout)
        layout.addWidget(output_group)
        
        # 번역 버튼
        translate_button = QPushButton("번역 시작")
        translate_button.setMinimumHeight(48)
        translate_button.setIcon(QIcon.fromTheme("media-playback-start"))
        translate_button.clicked.connect(self.start_translation)
        layout.addWidget(translate_button)
        
        # 진행 상황 표시
        progress_group = QGroupBox("진행 상황")
        progress_layout = QVBoxLayout()
        progress_layout.setContentsMargins(12, 20, 12, 12)
        progress_layout.setSpacing(10)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p% 완료")
        self.progress_bar.setMinimumHeight(25)
        progress_layout.addWidget(self.progress_bar)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(250)
        self.log_output.setStyleSheet("""
            QTextEdit {
                background-color: #f0f0f0;
                color: #333;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                padding: 8px;
                font-family: 'Menlo', 'Consolas', monospace;
                font-size: 13px;
            }
        """)
        progress_layout.addWidget(self.log_output)
        
        progress_group.setLayout(progress_layout)
        layout.addWidget(progress_group)
        
    def setup_youtube_tab(self):
        """YouTube 다운로드 탭 설정"""
        layout = QVBoxLayout(self.tab_youtube)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # URL 입력 그룹
        url_group = QGroupBox("YouTube URL")
        url_layout = QHBoxLayout()
        url_layout.setContentsMargins(12, 20, 12, 12)
        url_layout.setSpacing(8)
        
        self.youtube_url_edit = QLineEdit()
        self.youtube_url_edit.setPlaceholderText("YouTube 동영상 URL을 입력하세요")
        
        download_button = QPushButton("다운로드")
        download_button.setIcon(QIcon.fromTheme("go-down"))
        download_button.clicked.connect(self.start_youtube_download)
        
        url_layout.addWidget(self.youtube_url_edit, 4)
        url_layout.addWidget(download_button, 1)
        url_group.setLayout(url_layout)
        layout.addWidget(url_group)
        
        # 작업 설정 그룹
        options_group = QGroupBox("다운로드 후 작업")
        options_layout = QVBoxLayout()
        options_layout.setContentsMargins(15, 20, 15, 15)
        options_layout.setSpacing(10)
        
        self.option_extract = QCheckBox("자막 추출")
        self.option_extract.setChecked(True)
        options_layout.addWidget(self.option_extract)
        
        # 자막 추출에 MP3 사용 옵션 (MP3가 더 정확한 경우가 많음)
        self.option_use_mp3 = QCheckBox("자막 추출에 MP3 사용 (정확도 향상)")
        self.option_use_mp3.setChecked(True)
        options_layout.addWidget(self.option_use_mp3)
        
        self.option_translate = QCheckBox("자막 번역")
        self.option_translate.setChecked(True)
        options_layout.addWidget(self.option_translate)
        
        options_group.setLayout(options_layout)
        layout.addWidget(options_group)
        
        # 진행 상황 그룹
        yt_progress_group = QGroupBox("진행 상황")
        yt_progress_layout = QVBoxLayout()
        yt_progress_layout.setContentsMargins(12, 20, 12, 12)
        yt_progress_layout.setSpacing(10)
        
        # 프로그레스 바 추가
        self.yt_progress_bar = QProgressBar()
        self.yt_progress_bar.setTextVisible(True)
        self.yt_progress_bar.setFormat("%p% 완료")
        self.yt_progress_bar.setMinimumHeight(25)
        yt_progress_layout.addWidget(self.yt_progress_bar)
        
        self.yt_log_output = QTextEdit()
        self.yt_log_output.setReadOnly(True)
        self.yt_log_output.setMinimumHeight(300)
        self.yt_log_output.setStyleSheet("""
            QTextEdit {
                background-color: #f0f0f0;
                color: #333;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                padding: 8px;
                font-family: 'Menlo', 'Consolas', monospace;
                font-size: 13px;
            }
        """)
        yt_progress_layout.addWidget(self.yt_log_output)
        
        yt_progress_group.setLayout(yt_progress_layout)
        layout.addWidget(yt_progress_group)
        
    def setup_settings_tab(self):
        """설정 탭 설정"""
        # 스크롤 영역 생성
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        
        # 스크롤 영역에 들어갈 컨테이너 위젯
        settings_container = QWidget()
        
        # 컨테이너 위젯의 레이아웃
        layout = QVBoxLayout(settings_container)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # 스크롤 영역에 컨테이너 위젯 설정
        scroll_area.setWidget(settings_container)
        
        # 탭에 스크롤 영역 추가
        tab_layout = QVBoxLayout(self.tab_settings)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll_area)
        
        # 모델 설정 그룹
        model_group = QGroupBox("번역 모델 설정")
        model_layout = QVBoxLayout()
        model_layout.setContentsMargins(15, 20, 15, 15)
        model_layout.setSpacing(10)
        
        model_label = QLabel("Claude 모델:")
        model_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "claude-sonnet-4-5",
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-3-7-sonnet-20250219"
        ])
        self.model_combo.setMinimumHeight(36)
        self.model_combo.setStyleSheet("""
            QComboBox {
                padding: 8px;
                border-radius: 6px;
                font-size: 13px;
            }
        """)
        model_layout.addWidget(model_label)
        model_layout.addWidget(self.model_combo)
        
        # 추가 설명 레이블
        model_help = QLabel("* claude-3-7-sonnet: 균형 잡힌, claude-3-haiku: 빠른 & 저렴한, claude-3-opus: 최고 품질")
        model_help.setStyleSheet("color: #666666; font-style: italic; font-size: 13px;")
        model_help.setWordWrap(True)
        model_help.setMinimumHeight(30)
        model_layout.addWidget(model_help)
        
        # 배치 크기 설정
        batch_layout = QHBoxLayout()
        batch_layout.setSpacing(10)
        batch_label = QLabel("배치 크기:")
        batch_label.setStyleSheet("font-weight: bold;")
        self.batch_spin = QSpinBox()
        self.batch_spin.setMinimum(1)
        self.batch_spin.setMaximum(20)
        self.batch_spin.setValue(5)
        self.batch_spin.setStyleSheet("""
            QSpinBox {
                padding: 6px;
                border-radius: 6px;
            }
        """)
        batch_layout.addWidget(batch_label)
        batch_layout.addWidget(self.batch_spin)
        model_layout.addLayout(batch_layout)
        
        # 병렬 작업자 수 설정
        workers_layout = QHBoxLayout()
        workers_layout.setSpacing(10)
        workers_label = QLabel("병렬 작업자 수:")
        workers_label.setStyleSheet("font-weight: bold;")
        self.workers_spin = QSpinBox()
        self.workers_spin.setMinimum(1)
        self.workers_spin.setMaximum(10)
        self.workers_spin.setValue(3)
        self.workers_spin.setStyleSheet("""
            QSpinBox {
                padding: 6px;
                border-radius: 6px;
            }
        """)
        workers_layout.addWidget(workers_label)
        workers_layout.addWidget(self.workers_spin)
        model_layout.addLayout(workers_layout)
        
        model_group.setLayout(model_layout)
        layout.addWidget(model_group)
        
        # 디렉토리 설정 그룹
        dir_group = QGroupBox("파일 저장 위치 설정")
        dir_layout = QVBoxLayout()
        dir_layout.setContentsMargins(15, 20, 15, 15)
        dir_layout.setSpacing(10)
        
        # 다운로드 디렉토리 설정
        download_dir_layout = QHBoxLayout()
        download_dir_layout.setSpacing(10)
        download_dir_label = QLabel("기본 저장 위치:")
        download_dir_label.setStyleSheet("font-weight: bold;")
        self.download_dir_edit = QLineEdit()
        self.download_dir_edit.setText(self.download_directory)
        self.download_dir_edit.setReadOnly(True)
        
        download_dir_button = QPushButton("변경")
        download_dir_button.setIcon(QIcon.fromTheme("folder"))
        download_dir_button.clicked.connect(self.browse_download_directory)
        
        download_dir_layout.addWidget(download_dir_label)
        download_dir_layout.addWidget(self.download_dir_edit, 4)
        download_dir_layout.addWidget(download_dir_button, 1)
        dir_layout.addLayout(download_dir_layout)
        
        dir_note = QLabel("* 이 설정은 유튜브 동영상 다운로드와 자막 파일의 기본 저장 위치로 사용됩니다.")
        dir_note.setStyleSheet("color: #666666; font-style: italic;")
        dir_layout.addWidget(dir_note)
        
        dir_group.setLayout(dir_layout)
        layout.addWidget(dir_group)
        
        # API 키 설정 그룹
        api_group = QGroupBox("API 키 설정")
        api_layout = QVBoxLayout()
        api_layout.setContentsMargins(15, 20, 15, 15)
        api_layout.setSpacing(10)
        
        anthropic_layout = QHBoxLayout()
        anthropic_layout.setSpacing(10)
        anthropic_label = QLabel("Anthropic API 키:")
        anthropic_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.anthropic_key_edit = QLineEdit()
        self.anthropic_key_edit.setPlaceholderText("ANTHROPIC_API_KEY 환경 변수 사용 중")
        self.anthropic_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.anthropic_key_edit.setMinimumHeight(40)
        anthropic_layout.addWidget(anthropic_label)
        anthropic_layout.addWidget(self.anthropic_key_edit)
        api_layout.addLayout(anthropic_layout)
        
        assembly_layout = QHBoxLayout()
        assembly_layout.setSpacing(10)
        assembly_label = QLabel("AssemblyAI API 키:")
        assembly_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.assembly_key_edit = QLineEdit()
        self.assembly_key_edit.setPlaceholderText("ASSEMBLYAI_API_KEY 환경 변수 사용 중")
        self.assembly_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.assembly_key_edit.setMinimumHeight(40)
        assembly_layout.addWidget(assembly_label)
        assembly_layout.addWidget(self.assembly_key_edit)
        api_layout.addLayout(assembly_layout)
        
        api_note = QLabel("* API 키는 애플리케이션이 실행되는 동안만 유효합니다. 앱 종료 시 저장되지 않습니다.")
        api_note.setStyleSheet("color: #666666; font-style: italic;")
        api_layout.addWidget(api_note)
        
        api_group.setLayout(api_layout)
        layout.addWidget(api_group)
        
        # 저장 버튼
        save_button = QPushButton("설정 저장")
        save_button.setMinimumHeight(40)
        save_button.setIcon(QIcon.fromTheme("document-save"))
        save_button.clicked.connect(self.save_settings)
        layout.addWidget(save_button)
        
        # 정보 그룹
        info_group = QGroupBox("앱 정보")
        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(15, 20, 15, 15)
        info_layout.setSpacing(10)
        
        info_text = QLabel(
            "자막 번역 도구 v1.1\n\n"
            "이 앱은 영어 SRT 자막을 한국어로 번역하고, 비디오 파일 또는 YouTube 동영상에서 자막을 추출하는 기능을 제공합니다.\n"
            "Claude API를 사용하여 고품질 번역을 제공합니다.\n\n"
            "revfactory © 2024 All Rights Reserved"
        )
        info_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_text.setWordWrap(True)
        info_text.setStyleSheet("""
            QLabel {
                padding: 15px;
                background-color: #f0f0f0;
                border-radius: 8px;
                color: #555;
            }
        """)
        info_layout.addWidget(info_text)
        
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
        
        # 빈 공간 추가
        layout.addStretch(1)
    
    def load_config(self):
        """설정 로드"""
        config = SubtitleTranslationConfig()
        
        # UI에 설정 적용
        self.model_combo.setCurrentText(config.model)
        self.batch_spin.setValue(config.batch_size)
        self.workers_spin.setValue(config.max_workers)
        
        return config
        
    def load_directories_from_config(self):
        """설정 파일에서 디렉토리 정보 로드"""
        try:
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    
                    # 다운로드 디렉토리 설정이 있으면 로드
                    if 'download_directory' in config_data:
                        self.download_directory = config_data['download_directory']
        except Exception as e:
            print(f"디렉토리 설정 로드 중 오류: {e}")
            # 오류 발생 시 기본값 사용
    
    def save_settings(self):
        """설정 저장"""
        try:
            # 설정 객체 업데이트
            self.config.model = self.model_combo.currentText()
            self.config.batch_size = self.batch_spin.value()
            self.config.max_workers = self.workers_spin.value()
            
            # API 키 환경 변수 설정
            if self.anthropic_key_edit.text():
                os.environ['ANTHROPIC_API_KEY'] = self.anthropic_key_edit.text()
            
            if self.assembly_key_edit.text():
                os.environ['ASSEMBLYAI_API_KEY'] = self.assembly_key_edit.text()
            
            # 설정 파일 생성
            config_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
            config_data = {
                "model": self.config.model,
                "batch_size": self.config.batch_size,
                "max_tokens": self.config.max_tokens,
                "max_workers": self.config.max_workers,
                "input_token_cost": self.config.input_token_cost,
                "output_token_cost": self.config.output_token_cost,
                "download_directory": self.download_directory
            }
            
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2)
            
            QMessageBox.information(self, "설정 저장", "설정이 성공적으로 저장되었습니다.")
            
        except Exception as e:
            QMessageBox.warning(self, "오류", f"설정 저장 중 오류가 발생했습니다: {str(e)}")
    
    def browse_input_file(self):
        """입력 파일 선택 다이얼로그"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "자막 파일 선택", self.download_directory, "SRT 파일 (*.srt);;모든 파일 (*)"
        )
        
        if file_path:
            self.input_file_edit.setText(file_path)
            
            # 출력 파일 경로 자동 생성
            base, ext = os.path.splitext(os.path.basename(file_path))
            output_file_name = f"{base}_ko{ext}"
            output_dir = os.path.dirname(os.path.abspath(file_path))
            output_file = os.path.join(output_dir, output_file_name)
            
            self.output_file_edit.setText(output_file)
    
    def browse_output_file(self):
        """출력 파일 선택 다이얼로그"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "출력 파일 선택", self.output_file_edit.text() or self.download_directory, "SRT 파일 (*.srt);;모든 파일 (*)"
        )
        
        if file_path:
            self.output_file_edit.setText(file_path)
            
    def browse_download_directory(self):
        """다운로드 디렉토리 선택 다이얼로그"""
        dir_path = QFileDialog.getExistingDirectory(
            self, "기본 저장 위치 선택", self.download_directory, 
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks
        )
        
        if dir_path:
            self.download_directory = dir_path
            self.download_dir_edit.setText(dir_path)
            
    # on_format_option_changed 메소드 제거
    
    def start_translation(self):
        """번역 시작"""
        input_file = self.input_file_edit.text()
        output_file = self.output_file_edit.text()
        
        if not input_file:
            QMessageBox.warning(self, "경고", "번역할 자막 파일을 선택해주세요.")
            return
        
        if not os.path.exists(input_file):
            QMessageBox.warning(self, "경고", f"파일을 찾을 수 없습니다: {input_file}")
            return
        
        if not output_file:
            # 출력 파일 경로 자동 생성
            base, ext = os.path.splitext(os.path.basename(input_file))
            output_file_name = f"{base}_ko{ext}"
            output_dir = os.path.dirname(os.path.abspath(input_file))
            output_file = os.path.join(output_dir, output_file_name)
            self.output_file_edit.setText(output_file)
        
        # 표준 출력이 이미 리다이렉트되어 있는지 확인
        if not hasattr(self, 'stdout_redirect') or sys.stdout == sys.__stdout__:
            # 로그 출력 초기화
            self.log_output.clear()
            
            # 로그 출력 리다이렉트
            self.stdout_redirect = RedirectOutput(self.log_output)
            sys.stdout = self.stdout_redirect
            sys.stderr = self.stdout_redirect
        
        # 진행 바 초기화
        self.progress_bar.setValue(0)
        
        # 번역 스레드 시작
        self.translator_thread = TranslationThread(input_file, output_file, self.config)
        self.translator_thread.update_progress.connect(self.update_progress)
        self.translator_thread.update_status.connect(self.update_status)
        self.translator_thread.finished_signal.connect(self.translation_finished)
        self.translator_thread.start()
        
        # UI 상태 업데이트
        self.statusBar().showMessage("번역 진행 중...")
    
    def update_progress(self, current, total):
        """번역 진행 상황 업데이트"""
        progress = int((current / total) * 100)
        self.progress_bar.setValue(progress)
    
    def update_youtube_progress(self, percent):
        """YouTube 다운로드 진행 상황 업데이트"""
        self.yt_progress_bar.setValue(int(percent))
    
    def update_extract_progress(self, percent):
        """자막 추출 진행 상황 업데이트"""
        self.extract_progress_bar.setValue(int(percent))
        
        # 진행 단계에 따른 설명 텍스트 업데이트
        if percent < 30:
            self.extract_progress_bar.setFormat(f"%p% - 파일 업로드 중...")
        elif percent < 90:
            self.extract_progress_bar.setFormat(f"%p% - 음성 인식 중...")
        elif percent < 100:
            self.extract_progress_bar.setFormat(f"%p% - 자막 파일 생성 중...")
        else:
            self.extract_progress_bar.setFormat("%p% 완료")
    
    def update_status(self, message):
        """상태 메시지 업데이트"""
        self.statusBar().showMessage(message)
        self.log_output.append(message)
        self.yt_log_output.append(message)
    
    def translation_finished(self, success, message):
        """번역 완료 처리"""
        # 표준 출력 안전하게 복원
        try:
            if hasattr(self, 'stdout_redirect') and sys.stdout != sys.__stdout__:
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__
        except:
            pass  # 복원 실패 시 무시
        
        if success:
            QMessageBox.information(self, "번역 완료", f"번역이 완료되었습니다.\n\n{message}")
            self.statusBar().showMessage("번역 완료")
            self.progress_bar.setValue(100)
        else:
            QMessageBox.warning(self, "번역 실패", f"번역 중 오류가 발생했습니다:\n{message}")
            self.statusBar().showMessage("번역 실패")
            self.progress_bar.setValue(0)
    
    def start_youtube_download(self):
        """YouTube 다운로드 시작"""
        youtube_url = self.youtube_url_edit.text()
        
        if not youtube_url:
            QMessageBox.warning(self, "경고", "YouTube URL을 입력해주세요.")
            return
        
        # 로그 출력 초기화
        self.yt_log_output.clear()
        
        # 진행 바 초기화
        self.yt_progress_bar.setValue(0)
        
        # 로그 출력 리다이렉트
        self.stdout_redirect = RedirectOutput(self.yt_log_output)
        sys.stdout = self.stdout_redirect
        sys.stderr = self.stdout_redirect
        
        # 다운로드할 비디오와 오디오 파일 경로를 저장할 속성 초기화
        self.downloaded_video = None
        self.downloaded_audio = None
        self.download_tasks_completed = 0
        self.download_tasks_total = 2  # 비디오와 오디오 모두 다운로드
        
        # 비디오 다운로드 시작
        self.youtube_thread = YoutubeDownloadThread(
            youtube_url, 
            self.download_directory,
            extract_audio=False
        )
        self.youtube_thread.update_status.connect(self.update_status)
        self.youtube_thread.update_progress.connect(self.update_youtube_progress)
        self.youtube_thread.finished_signal.connect(self.youtube_video_download_finished)
        self.youtube_thread.start()
        self.statusBar().showMessage("YouTube 동영상 다운로드 중... 완료 후 MP3를 다운로드합니다.")
    
    def youtube_video_download_finished(self, success, filename, message):
        """YouTube 비디오 다운로드 완료 처리"""
        if success:
            self.statusBar().showMessage("동영상 다운로드 완료")
            self.yt_progress_bar.setValue(100)  # 진행률 100%로 설정
            self.downloaded_video = filename
            self.yt_log_output.append(f"동영상 다운로드 완료: {filename}")
            
            # 다운로드 완료 태스크 증가
            self.download_tasks_completed += 1
            
            # MP3 다운로드 시작
            self.audio_thread = YoutubeDownloadThread(
                self.youtube_url_edit.text(), 
                self.download_directory,
                extract_audio=True
            )
            self.audio_thread.update_status.connect(self.update_status)
            self.audio_thread.update_progress.connect(self.update_youtube_progress)
            self.audio_thread.finished_signal.connect(self.youtube_audio_download_finished)
            self.audio_thread.start()
            self.statusBar().showMessage("YouTube 오디오(MP3) 다운로드 중...")
            self.yt_progress_bar.setValue(0)  # 진행률 초기화
        else:
            QMessageBox.warning(self, "다운로드 실패", f"YouTube 동영상 다운로드 중 오류가 발생했습니다:\n{message}")
            self.statusBar().showMessage("다운로드 실패")
            self.yt_progress_bar.setValue(0)  # 진행률 초기화
            self.download_tasks_completed += 1  # 실패했지만 작업은 완료된 것으로 처리
            
            # 비디오 다운로드가 실패했더라도 MP3 다운로드 시도
            self.audio_thread = YoutubeDownloadThread(
                self.youtube_url_edit.text(), 
                self.download_directory,
                extract_audio=True
            )
            self.audio_thread.update_status.connect(self.update_status)
            self.audio_thread.update_progress.connect(self.update_youtube_progress)
            self.audio_thread.finished_signal.connect(self.youtube_audio_download_finished)
            self.audio_thread.start()
            self.statusBar().showMessage("YouTube 오디오(MP3) 다운로드 중...")
                
    def youtube_audio_download_finished(self, success, filename, message):
        """YouTube 오디오 다운로드 완료 처리"""
        if success:
            self.statusBar().showMessage("MP3 다운로드 완료")
            self.yt_progress_bar.setValue(100)  # 진행률 100%로 설정
            self.downloaded_audio = filename
            self.yt_log_output.append(f"MP3 오디오 다운로드 완료: {filename}")
        else:
            QMessageBox.warning(self, "다운로드 실패", f"YouTube MP3 다운로드 중 오류가 발생했습니다:\n{message}")
            self.statusBar().showMessage("MP3 다운로드 실패")
            
        # 다운로드 완료 태스크 증가
        self.download_tasks_completed += 1
        
        # 모든 다운로드가 완료된 경우
        self.process_downloads_completed()
    
    def process_downloads_completed(self):
        """모든 다운로드 완료 후 처리"""
        # 모든 다운로드 태스크가 완료되었는지 확인
        if self.download_tasks_completed >= self.download_tasks_total:
            # 자막 추출 여부 확인
            if self.option_extract.isChecked():
                # 자막 추출에 사용할 파일 결정
                if self.option_use_mp3.isChecked() and self.downloaded_audio:
                    # MP3를 우선적으로 사용
                    self.start_subtitle_extraction(self.downloaded_audio)
                elif self.downloaded_video:
                    # 비디오 파일 사용
                    self.start_subtitle_extraction(self.downloaded_video)
                else:
                    QMessageBox.warning(self, "자막 추출 실패", "자막을 추출할 파일이 없습니다.")
            else:
                # 다운로드 완료 메시지 표시
                message = "다운로드가 완료되었습니다.\n"
                if self.downloaded_video:
                    message += f"비디오: {os.path.basename(self.downloaded_video)}\n"
                if self.downloaded_audio:
                    message += f"MP3: {os.path.basename(self.downloaded_audio)}"
                
                QMessageBox.information(self, "다운로드 완료", message)
    
    def start_subtitle_extraction(self, video_filename):
        """자막 추출 시작"""
        # 진행 바 초기화
        self.extract_progress_bar.setValue(0)
        self.extract_progress_bar.setFormat("자막 추출 시작...")
        
        self.extract_thread = ExtractSubtitleThread(video_filename)
        self.extract_thread.update_status.connect(self.update_status)
        self.extract_thread.update_progress.connect(self.update_extract_progress)
        self.extract_thread.finished_signal.connect(self.subtitle_extraction_finished)
        self.extract_thread.start()
        
        # UI 상태 업데이트
        self.statusBar().showMessage("자막 추출 중...")
    
    def subtitle_extraction_finished(self, success, filename, message):
        """자막 추출 완료 처리"""
        if success:
            self.statusBar().showMessage("자막 추출 완료")
            self.extracted_subtitle = filename
            
            if self.option_translate.isChecked():
                # 번역 탭으로 전환하고 파일 경로 설정
                self.tabs.setCurrentIndex(2)  # 번역 탭 인덱스 수정
                self.input_file_edit.setText(filename)
                
                # 출력 파일 경로 자동 생성
                base, ext = os.path.splitext(os.path.basename(filename))
                output_file_name = f"{base}_ko{ext}"
                output_dir = os.path.dirname(os.path.abspath(filename))
                output_file = os.path.join(output_dir, output_file_name)
                self.output_file_edit.setText(output_file)
                
                # 표준 출력 복원 (만약 리다이렉트되어 있다면)
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__
                
                # 번역 시작
                self.start_translation()
            else:
                QMessageBox.information(self, "자막 추출 완료", f"자막 추출이 완료되었습니다.\n파일: {filename}")
        else:
            QMessageBox.warning(self, "자막 추출 실패", f"자막 추출 중 오류가 발생했습니다:\n{message}")
            self.statusBar().showMessage("자막 추출 실패")
            
    def setup_extract_tab(self):
        """자막 추출 탭 설정"""
        layout = QVBoxLayout(self.tab_extract)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # 비디오 파일 선택 그룹
        file_group = QGroupBox("비디오 파일")
        file_layout = QHBoxLayout()
        file_layout.setContentsMargins(12, 20, 12, 12)
        file_layout.setSpacing(8)
        
        self.video_file_edit = QLineEdit()
        self.video_file_edit.setPlaceholderText("자막을 추출할 비디오 파일 선택")
        self.video_file_edit.setReadOnly(True)
        
        browse_button = QPushButton("찾아보기")
        browse_button.setIcon(QIcon.fromTheme("video-x-generic"))
        browse_button.clicked.connect(self.browse_video_file)
        
        file_layout.addWidget(self.video_file_edit, 4)
        file_layout.addWidget(browse_button, 1)
        file_group.setLayout(file_layout)
        layout.addWidget(file_group)
        
        # 추출 설정 그룹
        options_group = QGroupBox("자막 추출 후 작업")
        options_layout = QVBoxLayout()
        options_layout.setContentsMargins(15, 20, 15, 15)
        options_layout.setSpacing(10)
        
        self.option_translate_after_extract = QCheckBox("자막 추출 후 번역하기")
        self.option_translate_after_extract.setChecked(True)
        options_layout.addWidget(self.option_translate_after_extract)
        
        options_group.setLayout(options_layout)
        layout.addWidget(options_group)
        
        # 추출 버튼
        extract_button = QPushButton("자막 추출 시작")
        extract_button.setMinimumHeight(48)
        extract_button.setIcon(QIcon.fromTheme("media-playback-start"))
        extract_button.clicked.connect(self.start_extraction_only)
        layout.addWidget(extract_button)
        
        # 진행 상황 표시
        progress_group = QGroupBox("진행 상황")
        progress_layout = QVBoxLayout()
        progress_layout.setContentsMargins(12, 20, 12, 12)
        progress_layout.setSpacing(10)
        
        # 자막 추출 진행 바 추가
        self.extract_progress_bar = QProgressBar()
        self.extract_progress_bar.setTextVisible(True)
        self.extract_progress_bar.setFormat("대기 중...")
        self.extract_progress_bar.setMinimumHeight(25)
        progress_layout.addWidget(self.extract_progress_bar)
        
        self.extract_log_output = QTextEdit()
        self.extract_log_output.setReadOnly(True)
        self.extract_log_output.setMinimumHeight(280)
        self.extract_log_output.setStyleSheet("""
            QTextEdit {
                background-color: #f0f0f0;
                color: #333;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                padding: 8px;
                font-family: 'Menlo', 'Consolas', monospace;
                font-size: 13px;
            }
        """)
        progress_layout.addWidget(self.extract_log_output)
        
        progress_group.setLayout(progress_layout)
        layout.addWidget(progress_group)
        
    def browse_video_file(self):
        """비디오 파일 선택 다이얼로그"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "비디오 파일 선택", self.download_directory, 
            "비디오 파일 (*.mp4 *.mkv *.avi *.mov *.webm *.mp3);;모든 파일 (*)"
        )
        
        if file_path:
            self.video_file_edit.setText(file_path)
            
    def start_extraction_only(self):
        """단독 자막 추출 시작"""
        video_file = self.video_file_edit.text()
        
        if not video_file:
            QMessageBox.warning(self, "경고", "자막을 추출할 비디오 파일을 선택해주세요.")
            return
            
        if not os.path.exists(video_file):
            QMessageBox.warning(self, "경고", f"파일을 찾을 수 없습니다: {video_file}")
            return
            
        # 로그 출력 초기화
        self.extract_log_output.clear()
        
        # 진행 바 초기화
        self.extract_progress_bar.setValue(0)
        self.extract_progress_bar.setFormat("자막 추출 시작...")
        
        # 로그 출력 리다이렉트
        self.stdout_redirect = RedirectOutput(self.extract_log_output)
        sys.stdout = self.stdout_redirect
        sys.stderr = self.stdout_redirect
        
        # 자막 추출 스레드 시작
        self.extract_only_thread = ExtractSubtitleThread(video_file)
        self.extract_only_thread.update_status.connect(self.update_status)
        self.extract_only_thread.update_progress.connect(self.update_extract_progress)
        self.extract_only_thread.finished_signal.connect(self.extraction_only_finished)
        self.extract_only_thread.start()
        
        # UI 상태 업데이트
        self.statusBar().showMessage("자막 추출 중...")
        
    def extraction_only_finished(self, success, filename, message):
        """단독 자막 추출 완료 처리"""
        # 표준 출력 안전하게 복원
        try:
            if hasattr(self, 'stdout_redirect') and sys.stdout != sys.__stdout__:
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__
        except:
            pass  # 복원 실패 시 무시
        
        if success:
            self.statusBar().showMessage("자막 추출 완료")
            
            if self.option_translate_after_extract.isChecked():
                # 번역 탭으로 전환하고 파일 경로 설정
                self.tabs.setCurrentIndex(2)  # 번역 탭 인덱스 수정
                self.input_file_edit.setText(filename)
                
                # 출력 파일 경로 자동 생성
                base, ext = os.path.splitext(os.path.basename(filename))
                output_file_name = f"{base}_ko{ext}"
                output_dir = os.path.dirname(os.path.abspath(filename))
                output_file = os.path.join(output_dir, output_file_name)
                self.output_file_edit.setText(output_file)
                
                # 번역 시작
                self.start_translation()
            else:
                QMessageBox.information(self, "자막 추출 완료", f"자막 추출이 완료되었습니다.\n파일: {filename}")
        else:
            QMessageBox.warning(self, "자막 추출 실패", f"자막 추출 중 오류가 발생했습니다:\n{message}")
            self.statusBar().showMessage("자막 추출 실패")


def main():
    app = QApplication(sys.argv)
    
    # 앱 스타일 설정
    app.setStyle("Fusion")
    
    # 폰트 설정
    font = QFont("Pretendard, Apple SD Gothic Neo, Malgun Gothic", 11)
    app.setFont(font)
    
    window = SubtitleTranslatorApp()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
