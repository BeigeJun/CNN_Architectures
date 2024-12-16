import torch
import torch.nn as nn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
import optuna

def _make_divisible(v, divisor=8, min_value=None):
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    if new_v < 0.9 * v:
        new_v += divisor
    return int(new_v)

class h_swish(nn.Module):
    def __init__(self):
        super(h_swish, self).__init__()
        self.relu6 = nn.ReLU6()

    def forward(self, x):
        return x * (self.relu6(x + 3) / 6)

class inverted_residual_block(nn.Module):
    def __init__(self, i, t, o, k, s, re=False, se=False):
        super(inverted_residual_block, self).__init__()
        expansion = int(i * t)
        if re:
            nonlinear = nn.ReLU6()
        else:
            nonlinear = h_swish()

        self.se = se
        self.conv = nn.Sequential(
            nn.Conv2d(i, expansion, 1, 1),
            nn.BatchNorm2d(expansion),
            nonlinear
        )
        self.dconv = nn.Sequential(
            nn.Conv2d(expansion, expansion, k, s, k // 2, groups=expansion),
            nn.BatchNorm2d(expansion),
            nonlinear
        )
        self.semodule = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(expansion, _make_divisible(expansion // 4), 1, 1),
            nn.ReLU(),
            nn.Conv2d(_make_divisible(expansion // 4), expansion, 1, 1),
            h_swish()
        )
        self.linearconv = nn.Sequential(
            nn.Conv2d(expansion, o, 1, 1),
            nn.BatchNorm2d(o)
        )
        self.shortcut = (i == o and s == 1)

    def forward(self, x):
        out = self.conv(x)
        out = self.dconv(out)
        if self.se:
            out *= self.semodule(out)
        out = self.linearconv(out)
        if self.shortcut:
            out += x
        return out

class mobilenetv3(nn.Module):
    def __init__(self, ver=0, w=1.0):
        super(mobilenetv3, self).__init__()
        large = [
            [1, 16, 3, 1, False, False],
            [4, 24, 3, 2, False, False],
            [3, 24, 3, 1, False, False],
            [3, 40, 5, 2, False, True],
            [3, 40, 5, 1, False, True],
            [3, 40, 5, 1, False, True],
            [6, 80, 3, 2, True, False],
            [2.5, 80, 3, 1, True, False],
            [2.4, 80, 3, 1, True, False],
            [2.4, 80, 3, 1, True, False],
            [6, 112, 3, 1, True, True],
            [6, 112, 3, 1, True, True],
            [6, 160, 5, 2, True, True],
            [6, 160, 5, 1, True, True],
            [6, 160, 5, 1, True, True]
        ]
        small = [
            [1, 16, 3, 2, False, True],
            [4, 24, 3, 2, False, False],
            [11.0 / 3.0, 24, 3, 1, False, False],
            [4, 40, 5, 2, True, True],
            [6, 40, 5, 1, True, True],
            [6, 40, 5, 1, True, True],
            [3, 48, 5, 1, True, True],
            [3, 48, 5, 1, True, True],
            [6, 96, 5, 2, True, True],
            [6, 96, 5, 1, True, True],
            [6, 96, 5, 1, True, True],
        ]
        in_channels = _make_divisible(16 * w)
        self.conv1 = nn.Sequential(
            nn.Conv2d(3, in_channels, 3, 2, 1),
            nn.BatchNorm2d(int(16 * w)),
            nn.ReLU6()
        )
        if ver == 0:
            stack = large
            last = 1280
        else:
            stack = small
            last = 1024
        layers = []
        for i in range(len(stack)):
            out_channels = _make_divisible(stack[i][1] * w)
            layers.append(
                inverted_residual_block(in_channels, stack[i][0], out_channels, stack[i][2], stack[i][3], stack[i][4],
                                        stack[i][5]))
            in_channels = out_channels
        self.stack = nn.Sequential(*layers)
        self.last = nn.Sequential(
            nn.Conv2d(in_channels, out_channels * 6, 1, 1),
            nn.BatchNorm2d(out_channels * 6),
            h_swish(),
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(out_channels * 6, last, 1, 1),
            h_swish(),
            nn.Conv2d(last, 10, 1, 1)
        )

    def forward(self, x):
        x = self.conv1(x)
        x = self.stack(x)
        x = self.last(x)
        x = x.view(x.size(0), -1)
        return x

def dwise_conv(ch_in, stride=1):
    return nn.Sequential(
        nn.Conv2d(ch_in, ch_in, kernel_size=3, padding=1, stride=stride, groups=ch_in, bias=False),
        nn.BatchNorm2d(ch_in),
        nn.ReLU6(inplace=True),
    )

def conv1x1(ch_in, ch_out):
    return nn.Sequential(
        nn.Conv2d(ch_in, ch_out, kernel_size=1, padding=0, stride=1, bias=False),
        nn.BatchNorm2d(ch_out),
        nn.ReLU6(inplace=True)
    )

def conv3x3(ch_in, ch_out, stride):
    return nn.Sequential(
        nn.Conv2d(ch_in, ch_out, kernel_size=3, padding=1, stride=stride, bias=False),
        nn.BatchNorm2d(ch_out),
        nn.ReLU6(inplace=True)
    )

class InvertedBlock(nn.Module):
    def __init__(self, ch_in, ch_out, expand_ratio, stride):
        super(InvertedBlock, self).__init__()
        self.stride = stride
        assert stride in [1, 2]
        hidden_dim = ch_in * expand_ratio
        self.use_res_connect = self.stride == 1 and ch_in == ch_out
        layers = []
        if expand_ratio != 1:
            layers.append(conv1x1(ch_in, hidden_dim))
        layers.extend([
            dwise_conv(hidden_dim, stride=stride),
            conv1x1(hidden_dim, ch_out)
        ])
        self.layers = nn.Sequential(*layers)

    def forward(self, x):
        if self.use_res_connect:
            return x + self.layers(x)
        else:
            return self.layers(x)

class MobileNetV2(nn.Module):
    def __init__(self, ch_in=3, n_classes=10, alpha=1.0):
        super(MobileNetV2, self).__init__()
        self.configs = [
            [1, 16, 1, 1],
            [6, 24, 2, 2],
            [6, 32, 3, 2],
            [6, 64, 4, 2],
            [6, 96, 3, 1],
            [6, 160, 3, 2],
            [6, 320, 1, 1]
        ]
        input_channel = int(32 * alpha)
        last_channel = int(1280 * alpha)
        self.stem_conv = conv3x3(ch_in, input_channel, stride=2)
        layers = []
        for t, c, n, s in self.configs:
            output_channel = int(c * alpha)
            for i in range(n):
                stride = s if i == 0 else 1
                layers.append(InvertedBlock(ch_in=input_channel, ch_out=output_channel, expand_ratio=t, stride=stride))
                input_channel = output_channel
        self.layers = nn.Sequential(*layers)
        self.last_conv = conv1x1(input_channel, last_channel)
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Sequential(
            nn.Dropout2d(0.2),
            nn.Linear(last_channel, n_classes)
        )

    def forward(self, x):
        x = self.stem_conv(x)
        x = self.layers(x)
        x = self.last_conv(x)
        x = self.avg_pool(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x

def objective(trial):
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ])

    trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
    trainloader = DataLoader(trainset, batch_size=128, shuffle=True, num_workers=2)
    testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
    testloader = DataLoader(testset, batch_size=100, shuffle=False, num_workers=2)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    alpha = trial.suggest_float("alpha", 0.5, 1.5)
    lr = trial.suggest_float("lr", 1e-4, 1e-2, log=True)
    optimizer_name = trial.suggest_categorical("optimizer", ["Adam", "SGD"])

    model = mobilenetv3(ver=0, w=alpha).to(device)

    criterion = nn.CrossEntropyLoss()

    if optimizer_name == "Adam":
        optimizer = optim.Adam(model.parameters(), lr=lr)
    else:
        optimizer = optim.SGD(model.parameters(), lr=lr, momentum=0.9)

    num_epochs = 10
    for epoch in range(num_epochs):
        model.train()
        running_loss = 0.0
        for i, data in enumerate(trainloader, 0):
            inputs, labels = data
            inputs, labels = inputs.to(device), labels.to(device)

            optimizer.zero_grad()

            outputs = model(inputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

            running_loss += loss.item()

        model.eval()
        correct = 0
        total = 0
        with torch.no_grad():
            for data in testloader:
                images, labels = data
                images, labels = images.to(device), labels.to(device)
                outputs = model(images)
                _, predicted = torch.max(outputs.data, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()

    accuracy = 100 * correct / total
    return accuracy

def main():
    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=50)

    print("Best trial:")
    trial = study.best_trial
    print(f"  Accuracy: {trial.value}")
    print("  Best hyperparameters: ", trial.params)

if __name__ == '__main__':
    main()