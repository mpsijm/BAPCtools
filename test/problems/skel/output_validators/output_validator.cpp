#include <iostream>
#include <fstream>

int main(int argc, char *argv[]) {
    std::ifstream team_ans(argv[2]);

    double answer;
    return (team_ans >> answer) ? 42 : 43;
}
