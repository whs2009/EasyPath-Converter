// Native batch UI. The worker receives values, never shell command strings.
#include <FL/Fl.H>
#include <FL/Fl_Double_Window.H>
#include <FL/Fl_Hold_Browser.H>
#include <FL/Fl_Native_File_Chooser.H>
#include <FL/Fl_Input.H>
#include <FL/Fl_Check_Button.H>
#include <FL/Fl_Choice.H>
#include <FL/Fl_Button.H>
#include <FL/Fl_Box.H>
#include <FL/Fl_Group.H>
#include <FL/Fl_Progress.H>
#include <FL/fl_ask.H>
#include <algorithm>
#include <atomic>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#if defined(_WIN32)
#define EXPORT extern "C" __declspec(dllexport)
#else
#define EXPORT extern "C" __attribute__((visibility("default")))
#endif
namespace fs = std::filesystem;
using Run = int (*)(const char*, const char*, const char*, const char*, int, int, int, int,
                    const char*, int (*)(), void (*)(double), char*, int);
static std::atomic<bool> canceled;
static std::atomic<double> progress;
static int stopping(){return canceled.load();}
static void advanced(double value){progress.store(value);}
struct Row {std::string path,name,facts,status;};
struct Update {unsigned index; std::string status;};
class Desktop {
public:
    Fl_Double_Window win{830,570,"EasyPath | Format Converter"};
    Fl_Group controls{0,0,830,465};
    Fl_Hold_Browser list{20,48,590,175,"Input slides"};
    Fl_Button add{625,48,180,32,"Add files..."},remove{625,88,180,32,"Remove selected"};
    Fl_Button facts{625,128,180,32,"DICOM facts..."};
    Fl_Input stem{160,235,450,30,"Output name"};
    Fl_Input folder{160,282,450,30,"Destination folder"};
    Fl_Button browse{625,282,180,30,"Choose folder..."};
    Fl_Choice format{160,329,178,30,"Format"},bits{397,329,60,30,"Bits"},loss{541,329,60,30,"Error %"},distance{700,329,80,30,"Distance"};
    Fl_Check_Button srgb{160,378,630,28,"Assume sRGB if a source has no embedded color profile"};
    Fl_Box hint{20,419,790,30};
    Fl_Progress bar{20,475,580,28};
    Fl_Button start{625,474,180,32,"Convert files"};
    Fl_Box message{20,516,585,37};
    Fl_Button cancel{625,516,180,32,"Cancel"};
    Run engine; std::vector<Row> rows; std::thread worker;
    std::mutex mutex; std::vector<Update> changes;
    std::atomic<bool> finished{false};bool busy=false;
    Desktop(Run api):engine(api) {
        // Keep queue editing separate from the always-responsive progress and cancel controls.
        controls.remove(bar);controls.remove(start);controls.remove(message);controls.remove(cancel);
        controls.end();win.add(bar);win.add(start);win.add(message);win.add(cancel);win.end();
        win.size_range(830,570);win.resizable(nullptr);
        list.align(FL_ALIGN_TOP_LEFT);list.format_char(0);list.column_char(0);
        message.align(FL_ALIGN_INSIDE|FL_ALIGN_LEFT);hint.align(FL_ALIGN_INSIDE|FL_ALIGN_LEFT);
        format.add("EasyPath (.ep)|Lossless TIFF|Lossless DICOM WSI");format.value(0);
        for(int i=5;i<=8;i++)bits.add(std::to_string(i).c_str());bits.value(3);
        for(int i=0;i<=15;i++)loss.add(std::to_string(i).c_str());loss.value(5);
        for(int i=1;i<=15;i++)distance.add(std::to_string(i).c_str());distance.value(3);
        add.callback([](Fl_Widget*,void* u){static_cast<Desktop*>(u)->choose_files();},this);
        list.callback([](Fl_Widget*,void* u){static_cast<Desktop*>(u)->selection();},this);
        remove.callback([](Fl_Widget*,void* u){auto& d=*static_cast<Desktop*>(u);int i=d.list.value();if(i){d.rows.erase(d.rows.begin()+i-1);d.list.remove(i);d.selection();}},this);
        stem.when(FL_WHEN_CHANGED);
        stem.callback([](Fl_Widget*,void* u){auto& d=*static_cast<Desktop*>(u);int i=d.list.value();if(i){d.rows[i-1].name=d.stem.value();d.refresh(i-1);}},this);
        browse.callback([](Fl_Widget*,void* u){auto& d=*static_cast<Desktop*>(u);Fl_Native_File_Chooser c(Fl_Native_File_Chooser::BROWSE_DIRECTORY);if(!c.show())d.folder.value(c.filename());},this);
        facts.callback([](Fl_Widget*,void* u){auto& d=*static_cast<Desktop*>(u);int i=d.list.value();if(!i)return;Fl_Native_File_Chooser c;c.title("Verified DICOM source facts (JSON)");c.filter("JSON\t*.json");if(!c.show()){d.rows[i-1].facts=c.filename();d.refresh(i-1);}},this);
        for(auto c:{&format,&loss,&bits})c->callback([](Fl_Widget*,void* u){static_cast<Desktop*>(u)->options();},this);
        start.callback([](Fl_Widget*,void* u){static_cast<Desktop*>(u)->run();},this);
        cancel.callback([](Fl_Widget*,void*){canceled=true;});
        win.callback([](Fl_Widget*,void* u){auto& d=*static_cast<Desktop*>(u);if(d.busy){if(fl_choice("Cancel this conversion?","Keep converting","Cancel conversion",nullptr)==1)canceled=true;}else d.win.hide();},this);
        cancel.deactivate();bar.minimum(0);bar.maximum(1);selection();options();
    }
    void refresh(int idx){const auto& r=rows.at(idx);std::string line=fs::u8path(r.path).filename().u8string()+"   ->   "+r.name;
        if(!r.facts.empty())line+="   [DICOM facts]";if(!r.status.empty())line+="   "+r.status;list.text(idx+1,line.c_str());}
    void selection(){int i=list.value();if(i){stem.activate();stem.value(rows.at(i-1).name.c_str());}else{stem.value("");stem.deactivate();}}
    void choose_files(){Fl_Native_File_Chooser c(Fl_Native_File_Chooser::BROWSE_MULTI_FILE);c.title("Select whole-slide files");
        if(c.show())return;for(int i=0;i<c.count();i++){std::string path=c.filename(i);
            if(std::none_of(rows.begin(),rows.end(),[&](const Row& r){return r.path==path;})){
                rows.push_back({path,fs::u8path(path).stem().u8string(),{},{}});
                list.add("");refresh(static_cast<int>(rows.size()-1));}}
        if(!rows.empty()){list.value(static_cast<int>(rows.size()));selection();}}
    void options(){bool ep=format.value()==0;
        if(ep){bits.activate();loss.activate();}else{bits.deactivate();loss.deactivate();}
        if(ep&&loss.value())distance.activate();else distance.deactivate();
        if(format.value()==2)facts.activate();else facts.deactivate();
        hint.copy_label(!ep?(format.value()==2?"One DICOM instance set per slide. Select verified source facts for each input.":"Lossless pyramidal TIFF. Existing files are never replaced."):
            (bits.value()<3&&!loss.value()?"0% preserves the reduced precision, not the original eight-bit colors.":
            "8 bits and 0% is lossless. Positive limits include precision reduction and compression."));}
    void post(unsigned i,std::string text){std::lock_guard<std::mutex> hold(mutex);changes.push_back({i,std::move(text)});}
    void run(){if(busy)return;if(rows.empty()||!*folder.value()){fl_alert("Select input slides and a destination folder.");return;}
        std::string dir=folder.value(),fmt=std::vector<std::string>{"ep","tiff","dcm"}.at(format.value());
        for(const auto& r:rows)if(r.name.empty()||(fmt=="dcm"&&r.facts.empty())){fl_alert("Each input needs an output name and, for DICOM, a source facts file.");return;}
        int b=bits.value()+5,e=loss.value(),d=distance.value()+1,s=srgb.value();
        for(unsigned i=0;i<rows.size();i++){rows[i].status="Queued";refresh(i);}
        auto jobs=rows;canceled=false;finished=false;progress=0;busy=true;controls.deactivate();start.deactivate();cancel.activate();
        worker=std::thread([this,jobs,dir,fmt,b,e,d,s]{
            for(unsigned i=0;i<jobs.size();i++){
                if(stopping()){post(i,"Canceled");continue;}
                const auto& r=jobs[i];post(i,"Converting");progress=0;char error[2048]={};
                int code=engine(r.path.c_str(),dir.c_str(),r.name.c_str(),fmt.c_str(),b,e,d,s,r.facts.c_str(),stopping,advanced,error,sizeof error);
                post(i,!code?"Completed":stopping()?"Canceled":std::string("Failed: ")+error);
            }finished=true;
        });}
    void tick(){std::vector<Update> updates;{std::lock_guard<std::mutex> hold(mutex);updates.swap(changes);}
        for(auto& u:updates){rows[u.index].status=u.status;refresh(u.index);message.copy_label((rows[u.index].name+": "+u.status).c_str());}
        bar.value(progress.load());
        if(busy&&finished){worker.join();busy=false;controls.activate();start.activate();cancel.deactivate();options();selection();}}
};
EXPORT int converter_show(Run fn,int smoke){
    if(!fn)return 2;Fl::scheme("gtk+");Desktop ui(fn);
    if(smoke&2)ui.win.label("EasyPath Format Converter | Hospital Research - Nonprofit Use");
    ui.win.show();
    if(smoke&1){Fl::check();ui.win.hide();return ui.format.value()==0&&ui.bits.value()==3&&ui.loss.value()==5&&ui.distance.value()==3?0:1;}
    while(ui.win.shown()){Fl::wait(.12);ui.tick();}return 0;
}
