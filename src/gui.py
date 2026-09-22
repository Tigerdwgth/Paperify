import gradio as gr
import datetime
import logging
import os
from paperagent_workflow import generate_daily_arxiv_summary
def process_arxiv_summary(query, max_papers, long_or_short, status=gr.Progress()):
    """处理 arxiv 摘要生成"""
    try:
        # 输入验证
        if not query or not isinstance(query, str) or len(query.strip()) == 0:
            return "❌ 请输入有效的查询关键词！", None, None, gr.update(visible=False)
        
        if not isinstance(max_papers, (int, float)) or max_papers < 1 or max_papers > 20:
            return "❌ 最大论文数应为 1~20 的整数！", None, None, gr.update(visible=False)
            
        if long_or_short not in ["long", "short"]:
            return "❌ 摘要类型只能为 long 或 short！", None, None, gr.update(visible=False)
        
        # 准备工作
        status(0.1, desc="正在准备...")
        today = datetime.datetime.now()
        yesterday = today - datetime.timedelta(days=1)
        yesterday_str = yesterday.strftime(r"%Y-%m-%d")
        today_str = today.strftime(r"%Y-%m-%d")
        
        logging.info("开始处理: 查询='%s', 论文数=%d, 类型=%s", query, max_papers, long_or_short)
        logging.info("日期范围: %s (昨天) 到 %s (今天)", yesterday_str, today_str)
        
        # 生成视频
        status(0.3, desc="正在搜索论文...")
        status(0.5, desc="正在生成摘要...")
        status(0.7, desc="正在创建视频...")
        
        result = generate_daily_arxiv_summary(
            query=query.strip(), 
            max_papers=int(max_papers), 
            date=yesterday_str, 
            long_or_short=long_or_short
        )
        path, titles, cn_titles = result[:3]
        
        status(0.9, desc="处理完成，准备显示...")
        
        # 检查生成的文件
        if not path or not os.path.exists(path):
            return "❌ 视频生成失败：文件不存在", None, None, gr.update(visible=False)
            
        # 准备返回信息
        info_text = f"""✅ 视频生成成功！
📅 日期: {yesterday_str}
🔍 查询: {query}
📊 论文数: {max_papers}
📝 摘要类型: {long_or_short}
📁 文件路径: {path}

📰 论文标题:
{titles if titles else '无'}

🌏 中文标题:
{cn_titles if cn_titles else '无'}"""
        
        status(1.0, desc="完成！")
        logging.info("视频生成成功: %s", path)
        
        return info_text, path, path, gr.update(visible=True)
        
    except Exception as e:
        error_msg = f"❌ 处理过程中发生错误:\n{str(e)}\n\n请检查:\n1. 网络连接是否正常\n2. API 配置是否正确\n3. 查询关键词是否有效"
        logging.error("程序运行时发生异常: %s", e, exc_info=True)
        return error_msg, None, None, gr.update(visible=False)
    finally:
        logging.info("处理结束")


# 创建 Gradio 界面
with gr.Blocks(title="具身人机 Arxiv 视频生成器", theme=gr.themes.Soft(), css="""
.gradio-container {
    max-width: 1200px !important;
}
""") as demo:
    gr.Markdown("""
    # 🤖 具身人机 Arxiv 视频生成器
    
    **功能简介：** 输入关键词，自动搜索最新 Arxiv 论文，生成具身智能相关的摘要视频。
    
    **使用步骤：**
    1. 输入查询关键词（如：`cs.RO`、`embodied AI`、`robotics` 等）
    2. 选择论文数量和摘要类型
    3. 点击生成按钮，等待处理完成
    4. 在右侧预览生成的视频
    """)
    
    with gr.Row():
        # 左侧输入区域
        with gr.Column(scale=1):
            gr.Markdown("### 📝 参数设置")
            
            query_input = gr.Textbox(
                label="🔍 查询关键词", 
                placeholder="输入 arxiv 分类（如 cs.RO）或关键词（如 embodied AI）", 
                value="cs.RO", 
                lines=1,
                info="支持 arxiv 分类代码或自然语言关键词"
            )
            
            with gr.Row():
                max_papers_input = gr.Number(
                    label="📊 最大论文数", 
                    value=3, 
                    precision=0, 
                    minimum=1, 
                    maximum=20,
                    info="建议 1-5 篇，避免视频过长"
                )
                
                long_or_short_input = gr.Radio(
                    choices=["long", "short"], 
                    label="📝 摘要类型", 
                    value="long", 
                    info="long: 详细摘要; short: 简短摘要"
                )
            
            submit_btn = gr.Button(
                "🚀 生成视频", 
                variant="primary", 
                size="lg"
            )
            
            gr.Markdown("### 💡 常用查询示例")
            gr.Examples([
                ["cs.RO", 3, "long"],
                ["embodied AI", 2, "short"],
                ["robotics", 1, "long"],
                ["multimodal", 2, "short"],
                ["cs.AI", 5, "long"]
            ],
                inputs=[query_input, max_papers_input, long_or_short_input],
                label="点击示例快速开始"
            )
        
        # 右侧结果区域
        with gr.Column(scale=1):
            gr.Markdown("### 📋 处理结果")
            output_text = gr.Textbox(
                label="📄 生成信息", 
                lines=8, 
                interactive=False,
                placeholder="点击'生成视频'开始处理..."
            )

            video_output = gr.Video(label="🎬 视频预览")

            with gr.Group(visible=False) as download_group:
                file_output = gr.File(label="⬇️ 下载视频文件")

    submit_btn.click(
        process_arxiv_summary,
        inputs=[query_input, max_papers_input, long_or_short_input],
        outputs=[output_text, video_output, file_output, download_group],
        api_name="arxiv2bili",
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("app.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    try:
        demo.launch(
            show_error=True,
            server_name="0.0.0.0",
            server_port=int(os.environ.get("JSR_GUI_PORT", "7860")),
            share=False,
        )
    finally:
        logging.info("程序结束")
        logging.shutdown()
